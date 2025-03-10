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
Parser.add_argument('-t', '--threads', type=int, help='Number Of Concurrent Downloads (Doesn\'t Work)', default=1)
Args = Parser.parse_args()

# Setup
class Config:
	BaseURL = 'https://photos.google.com/'
	LoginURL = 'https://photos.google.com/login'
	PhotoURL = 'https://photos.google.com/lr/photo/'
	Profile = Path.home() / '.gphotosdl' / 'profile'
	DownloadDirectory = Path.home() / '.gphotosdl' / 'downloads'
	ServerPort = Args.port

	@classmethod
	def InitializeFolders(cls):
		Log.debug('Setting Up Directories')
		for Directory in [cls.Profile.parent, cls.Profile, cls.DownloadDirectory]:
			Directory.mkdir(parents=True, exist_ok=True, mode=0o755)
			Log.debug(f'Directory Created: {Directory}')
		if cls.Profile.exists():
			ProfileSize = GetFolderSize(cls.Profile)
			Log.debug(f'Profile Directory Size: {HumanizeBytes(ProfileSize)}')

# Logging Setup
class DownloadHighlighter(RegexHighlighter):
	base_style = 'browser.'
	highlights = [r'(?P<time>[\d.]+s)', r'\[(?P<error>error|Error)\]', r'\[(?P<warning>warning|Warning)\]', 
				  r'\[(?P<info>info|Info)\]', r'\[(?P<debug>debug|Debug)\]', r'(?P<size>\d+\.\d+ [KMGT]B)', 
				  r'\.\.\.(?P<hash>[a-zA-Z0-9-_]{10})', r'\[(?P<speed>\d+\.\d+ [KMGT]B/s)\]']
ThemeDict = { **{f'logging.level.{lvl}': col for lvl, col in zip(['debug', 'info', 'warning', 'error'], 
			 ['#B3D7EC', '#A0D6B4', '#F5D7A3', '#F5A3A3'])}, 
			  **{f'browser.{key}': val for key, val in {'time': '#F5D7A3', 'error': '#F5A3A3', 'warning': '#F5D7A3', 
			  'info': '#A0D6B4', 'debug': '#B3D7EC', 'size': '#A0D6B4', 'hash': '#B3D7EC', 'speed': '#D8BFD8'}.items()},
			  'log.time': 'bright_black' }
def InitLogging():
	Console = RichConsole(theme=Theme(ThemeDict), force_terminal=True, log_path=False, 
						  highlighter=DownloadHighlighter(), color_system='truecolor')
	ConsoleHandler = RichHandler(markup=True, rich_tracebacks=True, show_time=True, console=Console, 
								 show_path=False, omit_repeated_times=True, highlighter=DownloadHighlighter())
	ConsoleHandler.setFormatter(logging.Formatter('%(message)s', datefmt='[%H:%M:%S]'))
	logging.basicConfig(handlers=[ConsoleHandler], force=True, level=logging.DEBUG if Args.debug else logging.INFO)
	Log = logging.getLogger('rich')
	Log.handlers.clear(), Log.addHandler(ConsoleHandler), setattr(Log, 'propagate', False)
	Install(show_locals=True, console=Console, max_frames=1)
	return Console, Log
Console, Log = InitLogging()

# Humanize Bytes
def HumanizeBytes(Bytes):
	for Unit in ['B', 'KB', 'MB', 'GB', 'TB']:
		if Bytes < 1024 or Unit == 'TB':
			return f'{Bytes:.2f} {Unit}'
		Bytes /= 1024

def GetFolderSize(Path: Path) -> int:
	return sum(f.stat().st_size for f in Path.rglob('*') if f.is_file())

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
			ResponseQueue = queue.Queue()
			try:
				asyncio.run_coroutine_threadsafe(self.App.Receive(PhotoID, ResponseQueue), self.App.EventLoop)
				FilePath = ResponseQueue.get(timeout=90)
				with open(FilePath, 'rb') as File:
					self.send_response(200)
					self.end_headers()
					self.wfile.write(File.read())
				os.remove(FilePath)
				Log.debug(f'Deleted File: {FilePath}')
			except queue.Empty:
				Log.error(f'Download Timed Out: ...{PhotoID[-10:]}')
				self.send_response(504)
				self.end_headers()
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
		self.Queue = asyncio.Queue()
		self.GlobalQueue = queue.Queue()
		self.PagePool = []
		self.PageLock = asyncio.Lock()
		self.MaxConcurrent = Args.threads
		self.ActiveDownloads = 0

		self.ConfigInfo = {
			'Headless Mode': Args.headless,
			'Debug Mode': Args.debug,
			'Server Port': Config.ServerPort,
			'Allow Images': Args.allow_images,
			'Concurrent Downloads': self.MaxConcurrent
		}

		self.LoopThread = threading.Thread(target=self._RunEventLoop)
		self.LoopThread.daemon = True
		self.LoopThread.start()

		while not hasattr(self, 'IsInitialized') or not self.IsInitialized:
			time.sleep(0.1)

	def _RunEventLoop(self):
		asyncio.set_event_loop(self.EventLoop)
		self.EventLoop.run_forever()

	async def Initialize(self, Headless: bool):
		Log.info('Google Photos Downloader Configuration:')
		Log.debug('Note That Audio Isn\'t Muted On Videos')
		for Key, Value in self.ConfigInfo.items():
			Log.info(f'• {Key}: {Value}')

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
				i_know_what_im_doing=True, # I Don't
				block_images=Args.allow_images
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

		self.PagePool.append(self.Page)

		for i in range(1, self.MaxConcurrent):
			Log.debug(f'Creating Page {i+1}/{self.MaxConcurrent}')
			page = await self.Instance.new_page()
			await self.SetupPage(page)
			self.PagePool.append(page)

		Log.info(f'Created {self.MaxConcurrent} Browser Pages')
		Log.info('Ready To Receive Requests, Start Your Rclone! 🚀')
		self.StartServer()
		self.ProcessorTask = self.EventLoop.create_task(self.ProcessQueue())
		self.IsInitialized = True

	async def Setup(self):
		Log.debug('Setting Up Browser Timeouts')
		self.Page.context.set_default_timeout(0)
		self.Page.context.set_default_navigation_timeout(0)

	async def SetupPage(self, Page):
		Page.context.set_default_timeout(0)
		Page.context.set_default_navigation_timeout(0)

	async def Download(self, Page, PhotoID):
		Log.debug(f'Downloading Photo ID: ...{PhotoID[-10:]}')
		StartTime = time.time()
		await Page.goto(Config.PhotoURL + PhotoID)

		async with Page.expect_download() as Info:
			await Page.keyboard.press('Shift+D')

		Download = await Info.value
		FilePath = str(Config.DownloadDirectory / Download.suggested_filename)
		await Download.save_as(FilePath)

		FileSize = os.path.getsize(FilePath)
		DownloadTime = time.time() - StartTime
		Log.info(f'╭ Downloaded Photo ID: ...{PhotoID[-10:]} ({HumanizeBytes(FileSize)}) in {DownloadTime:.2f}s [{HumanizeBytes(FileSize / DownloadTime)}/s]')
		if len(Download.suggested_filename) > 40:
			Log.info(f'╰ {Download.suggested_filename[:40]}...')
		else:
			Log.info(f'╰ {Download.suggested_filename}')

		await Page.goto('about:blank')
		return FilePath

	def StartServer(self):
		try:
			Server = HTTPServer(('', Config.ServerPort), lambda *args: RequestHandler(*args, app=self))
			ServerThread = threading.Thread(target=Server.serve_forever)
			ServerThread.daemon = True
			ServerThread.start()
			Log.info(f'Server Started On Port {Config.ServerPort}')
			for Interface in self.GetNetworkInterfaces():
				Log.info(f'• Server Available At: http://{Interface}:{Config.ServerPort}')
		except OSError as E:
			Log.error(f'Failed To Start Server: {E}')
			os._exit(1)

	def GetNetworkInterfaces(self):
		import socket
		Interfaces = set()
		try:
			Interfaces.add(socket.gethostbyname(socket.gethostname()))
			if Args.debug:
				for Info in socket.getaddrinfo(socket.gethostname(), None):
					if Info[0] == socket.AF_INET:
						Interfaces.add(Info[4][0])
		except Exception:
			pass
		return sorted(Interfaces) or ['127.0.0.1']

	async def Receive(self, PhotoID, ResponseQueue=None):
		Log.debug(f'Received Photo ID: ...{PhotoID[-10:]}')
		await self.Queue.put((PhotoID, ResponseQueue))

	async def ProcessQueue(self):
		Tasks = set()
		try:
			while True:
				try:
					if not self.Queue.empty() and len(self.PagePool) > 0:
						PhotoData = await self.Queue.get()
						Task = asyncio.create_task(self.ProcessDownload(PhotoData))
						Tasks.add(Task)
						Task.add_done_callback(Tasks.discard)

					await asyncio.sleep(0.1)
				except playwright.async_api.Error:
					Log.error('Browser Connection Lost')
					break
		except Exception as E:
			Log.error(f'Queue Processor Error: {E}')

	async def ProcessDownload(self, PhotoData):
		PhotoID, ResponseQueue = PhotoData if isinstance(PhotoData, tuple) else (PhotoData, None)

		async with self.PageLock:
			if not self.PagePool:
				Log.debug(f'No Pages Available, Waiting For One To Free Up For ...{PhotoID[-10:]}')
				await self.Queue.put((PhotoID, ResponseQueue))
				return
			self.ActiveDownloads += 1
			Page = self.PagePool.pop()

		try:
			FilePath = await self.Download(Page, PhotoID)
			if ResponseQueue:
				ResponseQueue.put(FilePath)
			else:
				self.GlobalQueue.put(FilePath)
		except Exception as E:
			Log.error(f'Download error for ...{PhotoID[-10:]}: {E}')
			if ResponseQueue:
				ResponseQueue.put_nowait(None)
		finally:
			async with self.PageLock:
				self.PagePool.append(Page)
				self.ActiveDownloads -= 1
				Log.debug(f'Active Downloads: {self.ActiveDownloads}/{self.MaxConcurrent}')

	async def Close(self):
		try:
			if hasattr(self, 'Instance') and self.Instance:
				await self.Instance.close()
			if hasattr(self, 'PlaywrightInstance') and self.PlaywrightInstance:
				await self.PlaywrightInstance.stop()
		except playwright.async_api.Error:
			Log.warning('Browser Has Been Closed')

	def Shutdown(self):
		if hasattr(self, 'ProcessorTask') and self.ProcessorTask:
			self.ProcessorTask.cancel()
		if self.EventLoop and self.EventLoop.is_running():
			self.EventLoop.create_task(self.Close())
			self.EventLoop.call_soon_threadsafe(self.EventLoop.stop)
		self.LoopThread.join(timeout=5)
		Log.warning('Exiting')

if __name__ == '__main__':
	Instance = None
	try:
		Config.InitializeFolders()
		Log.info('Starting Browser')
		Instance = Photos(Args.headless)
		Log.info('Browser Started')

		try:
			while True:
				time.sleep(0.5)
		except KeyboardInterrupt:
			Log.warning('Keyboard Interrupt Detected')
		finally:
			if Instance:
				Instance.Shutdown()
				Log.info('Browser Shutdown Complete')

			try:
				for File in Config.DownloadDirectory.iterdir():
					if File.is_file():
						File.unlink()
						Log.debug(f'Deleted File: {File}')
				Config.DownloadDirectory.rmdir()
			except Exception as E:
				if Args.debug:
					Log.warning(f'Cleanup Error: {E}')
	except Exception as E:
		Log.error(f'[{E.__class__.__name__}] {E}')
		if Instance:
			Instance.Shutdown()
		raise