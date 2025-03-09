# Web Scraping
from browserforge.fingerprints import Screen
import playwright.async_api
import camoufox

# Standard Library
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading
import argparse
import asyncio
import queue
import time
import os

# Logging
from rich.console import Console as RichConsole
from rich.traceback import install as Install
from rich.highlighter import RegexHighlighter
from rich.logging import RichHandler
from rich.theme import Theme
import logging

# Arguments
Parser = argparse.ArgumentParser(description='Google Photos Downloader')
Parser.add_argument('-d', '--debug', action='store_true', help='Enable Debug Mode')
Parser.add_argument('-hl', '--headless', action='store_true', help='Enable Headless Mode')
Parser.add_argument('-p', '--port', type=int, help='Server Port', default=8282)
Parser.add_argument('-i', '--allow-images', action='store_true', help='Allow Image Loading')
Args = Parser.parse_args()

# Setup
class Config:
	BaseURL = 'https://photos.google.com/'
	LoginURL = 'https://photos.google.com/login'
	GPhotoURL = 'https://photos.google.com/lr/photo/'
	GPhotoURLReal = 'https://photos.google.com/photo/'
	Profile = Path(os.path.join(os.getcwd(), 'profile'))
	DownloadDirectory = Path(os.path.join(os.getcwd(), 'downloads'))
	ServerPort = Args.port
	QueueMaxWait = 30

# Logging Setup
class DownloadHighlighter(RegexHighlighter):
	base_style = 'browser.'
	highlights = [
		r'(?P<time>[\d.]+s)',
		r'\[(?P<error>error|Error)\]',
		r'\[(?P<warning>warning|Warning)\]',
		r'\[(?P<info>info|Info)\]',
		r'\[(?P<debug>debug|Debug)\]',
		r'(?P<size>\d+\.\d+ [KMGT]B)',
		r'\.\.\.(?P<hash>[a-zA-Z0-9-_]{10})'
	]

ThemeDict = {
	'log.time': 'bright_black',
	'logging.level.debug': '#B3D7EC',
	'logging.level.info': '#A0D6B4',
	'logging.level.warning': '#F5D7A3',
	'logging.level.error': '#F5A3A3',
	'browser.time': '#F5D7A3',
	'browser.error': '#F5A3A3',
	'browser.warning': '#F5D7A3',
	'browser.info': '#A0D6B4',
	'browser.debug': '#B3D7EC',
	'browser.size': '#A0D6B4',
	'browser.hash': '#B3D7EC',
}

def InitLogging():
	Console = RichConsole(theme=Theme(ThemeDict), force_terminal=True, log_path=False,
						 highlighter=DownloadHighlighter(), color_system='truecolor')
	ConsoleHandler = RichHandler(markup=True, rich_tracebacks=True, show_time=True,
								console=Console, show_path=False, omit_repeated_times=True,
								highlighter=DownloadHighlighter())
	ConsoleHandler.setFormatter(logging.Formatter('%(message)s', datefmt='[%H:%M:%S]'))
	logging.basicConfig(handlers=[ConsoleHandler], force=True, level=logging.DEBUG if Args.debug else logging.INFO)
	Log = logging.getLogger('rich')
	Log.handlers.clear()
	Log.addHandler(ConsoleHandler)
	Log.propagate = False
	Install(show_locals=True, console=Console, max_frames=1)
	return Console, Log
Console, Log = InitLogging()

Log.debug('Setting Up Directories')
Log.debug(f'Profile Directory: {Config.Profile}')
os.makedirs(Config.Profile, exist_ok=True)
Log.debug(f'Download Directory: {Config.DownloadDirectory}')
os.makedirs(Config.DownloadDirectory, exist_ok=True)

# Humanize Bytes
def HumanizeBytes(Bytes):
	for Unit in ['B', 'KB', 'MB', 'GB', 'TB']:
		if Bytes < 1024 or Unit == 'TB':
			return f'{Bytes:.2f} {Unit}'
		Bytes /= 1024

# Server
class RequestHandler(BaseHTTPRequestHandler):
	def __init__(self, *args, app=None, **kwargs):
		self.App = app
		super().__init__(*args, **kwargs)

	def do_GET(self):
		if self.path == '/':
			self.send_response(200)
			self.send_header('Content-Type', 'text/html')
			self.end_headers()
			self.wfile.write(b'<h1>Google Photos Downloader</h1>')
		elif self.path.startswith('/id/'):
			PhotoID = self.path[4:]
			try:
				asyncio.run_coroutine_threadsafe(self.App.Receive(PhotoID), self.App.EventLoop)
				while self.App.GlobalQueue.empty():
					time.sleep(0.1)
				FilePath = self.App.GlobalQueue.get()
				with open(FilePath, 'rb') as File:
					self.send_response(200)
					self.end_headers()
					self.wfile.write(File.read())
				os.remove(FilePath)
				Log.debug(f'Deleted File: {FilePath}')
			except Exception as E:
				Log.error(f'Download Failed: {E}')
				self.send_response(500)
				self.end_headers()

	def log_message(self, format, *args):
		Log.debug(f'{self.address_string()} - {format % args}')

# Browser
class Photos:
	def __init__(self, Headless: bool = False):
		self.EventLoop = asyncio.new_event_loop()
		self.InitTask = self.EventLoop.create_task(self.Initialize(Headless))
		self.ProcessorTask = None
		self.Queue = asyncio.Queue(maxsize=1)
		self.GlobalQueue = queue.Queue(maxsize=1)

		self.LoopThread = threading.Thread(target=self._RunEventLoop)
		self.LoopThread.daemon = True
		self.LoopThread.start()

		while not hasattr(self, 'IsInitialized') or not self.IsInitialized:
			time.sleep(0.1)

	def _RunEventLoop(self):
		asyncio.set_event_loop(self.EventLoop)
		self.EventLoop.run_forever()

	async def Initialize(self, Headless: bool):
		self.PlaywrightInstance = await playwright.async_api.async_playwright().start()
		self.Instance = await camoufox.AsyncNewBrowser(
			playwright=self.PlaywrightInstance,
			from_options=camoufox.launch_options(
				screen=Screen(
					min_width=1279,
					min_height=719,
					max_width=1280,
					max_height=720
				),
				window=[1280, 720],
				os=['windows', 'linux'],
				user_data_dir=str(Config.Profile),
				color_scheme='dark',
				headless=Headless,
				java_script_enabled=True,
				i_know_what_im_doing=True,
				block_images=not Args.allow_images,
				args=[]
			),
			persistent_context=True
		)
		self.Page = self.Instance.pages[0]
		await self.Setup()
		Log.info('Please Login To Google Photos')
		await self.Page.goto(Config.LoginURL)

		while not self.Page.url.startswith(Config.BaseURL):
			Log.debug(f'Current URL: {self.Page.url}')
			await asyncio.sleep(1)

		Log.info('Logged In')
		await self.Page.goto('about:blank')
		Log.info('Ready To Receive Requests, Start Your Rclone! 🚀')
		self.StartServer()
		self.ProcessorTask = self.EventLoop.create_task(self.ProcessQueue())
		self.IsInitialized = True

	async def Setup(self):
		Log.debug('Setting Up Browser Timeouts')
		self.Page.context.set_default_timeout(0)
		self.Page.context.set_default_navigation_timeout(0)

	async def Download(self, PhotoID):
		Log.debug(f'Downloading Photo ID: ...{PhotoID[-10:]}')
		await self.Page.goto(Config.GPhotoURL + PhotoID)

		async with self.Page.expect_download() as Info:
			await self.Page.keyboard.press('Shift+D')

		Download = await Info.value
		FilePath = str(Config.DownloadDirectory / Download.suggested_filename)
		await Download.save_as(FilePath)

		FileSize = os.path.getsize(FilePath)
		Log.info(f'Downloaded Photo ID: ...{PhotoID[-10:]} ({HumanizeBytes(FileSize)})')

		await self.Page.goto('about:blank')
		return FilePath

	def StartServer(self):
		Server = HTTPServer(('0.0.0.0', Config.ServerPort), lambda *args: RequestHandler(*args, app=self))
		ServerThread = threading.Thread(target=Server.serve_forever)
		ServerThread.daemon = True
		ServerThread.start()
		Log.debug(f'Server Started On Port {Config.ServerPort}')

	async def Receive(self, PhotoID):
		Log.debug(f'Received Photo ID: ...{PhotoID[-10:]}')
		await self.Queue.put(PhotoID)

	async def ProcessQueue(self):
		try:
			while True:
				if not self.Queue.empty():
					PhotoID = await self.Queue.get()
					FilePath = await self.Download(PhotoID)
					self.GlobalQueue.put(FilePath)
					self.Queue.task_done()
				await asyncio.sleep(0.1)
		except Exception as E:
			Log.error(f'Queue processor error: {E}')

	async def Close(self):
		try:
			if hasattr(self, 'Instance') and self.Instance:
				await self.Instance.close()
			if hasattr(self, 'PlaywrightInstance') and self.PlaywrightInstance:
				await self.PlaywrightInstance.stop()
		except playwright.async_api.Error:
			Log.warning('Browser Has Been Closed')

	def Shutdown(self):
		if self.EventLoop and self.EventLoop.is_running():
			self.EventLoop.create_task(self.Close())
			self.EventLoop.call_soon_threadsafe(self.EventLoop.stop)
		self.LoopThread.join(timeout=5)
		Log.warning('Exiting')

if __name__ == '__main__':
	Instance = None
	try:
		Log.info('Starting Browser')
		Instance = Photos(Args.headless)
		Log.info('Browser Started')

		try:
			while True:
				time.sleep(0.5)
		except KeyboardInterrupt:
			Log.warning('Keyboard Interrupt Detected - Force Exiting')
			if Instance:
				Instance.Shutdown()
			os._exit(0)
	except Exception as E:
		Log.error(f'[{E.__class__.__name__}] {E}')
		if Instance:
			Instance.Shutdown()
		os._exit(1)
	finally:
		for File in Config.DownloadDirectory.iterdir():
			if File.is_file():
				Log.debug(f'Deleted File: {File}')
				File.unlink()
		os.rmdir(Config.DownloadDirectory)