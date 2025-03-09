# Web Scraping
from browserforge.fingerprints import Screen
import playwright
import camoufox

# Standard Library
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading
import argparse
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

# Setup
class Config:
	BaseURL = 'https://photos.google.com/'
	LoginURL = 'https://photos.google.com/login'
	GPhotoURL = 'https://photos.google.com/lr/photo/'
	GPhotoURLReal = 'https://photos.google.com/photo/'
	Profile = Path(os.getcwd() + r'\profile')
	DownloadDirectory = Path(os.getcwd() + r'\downloads')
	ServerPort = 8282
	QueueMaxWait = 30

os.makedirs(Config.Profile, exist_ok=True)
os.makedirs(Config.DownloadDirectory, exist_ok=True)

# Logging Setup
class DownloadHighlighter(RegexHighlighter):
	base_style = 'browser.'
	highlights = [
		r'(?P<time>[\d.]+s)',
		r'\[(?P<error>error|Error)\]',
		r'\[(?P<warning>warning|Warning)\]',
		r'\[(?P<info>info|Info)\]',
		r'\[(?P<debug>debug|Debug)\]',
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
}

# Arguments
Parser = argparse.ArgumentParser(description='Google Photos Downloader')
Parser.add_argument('-d', '--debug', action='store_true', help='Enable Debug Mode')
Parser.add_argument('-hl', '--headless', action='store_true', help='Enable Headless Mode')
Args = Parser.parse_args()

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
				self.App.Receive(PhotoID)
				while self.App.GlobalQueue.empty():
					time.sleep(0.1)
				FilePath = self.App.GlobalQueue.get()
				with open(FilePath, 'rb') as File:
					self.send_response(200)
					self.end_headers()
					self.wfile.write(File.read())
				os.remove(FilePath)
				Log.info(f'Deleted File: {FilePath}')
			except Exception as E:
				Log.error(f'Download Failed: {E}')
				self.send_response(500)
				self.end_headers()
	
	def log_message(self, format, *args):
		Log.debug(f'{self.address_string()} - {format % args}')

# Browser
class Photos:
	def __init__(self, Headless: bool = False):
		self.PlaywrightInstance = playwright.sync_api.sync_playwright().start()
		self.Instance = camoufox.NewBrowser(
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
				block_images=True,
				args=['--mute-audio', '--disable-audio-output']
			),
			persistent_context=True
		)
		self.Page = self.Instance.pages[0]
		self.Queue = queue.Queue(maxsize=1)
		self.GlobalQueue = queue.Queue(maxsize=1)
		self.Setup()
		Log.info('Please Login To Google Photos')
		self.Page.goto(Config.LoginURL)
		while not self.Page.url.startswith(Config.BaseURL):
			Log.debug(f'Current URL: {self.Page.url}')
			time.sleep(1)
		Log.info('Logged In')
		self.Page.goto('about:blank')
		self.StartServer()
	
	def Setup(self):
		Log.debug('Setting Up Browser Timeouts')
		self.Page.context.set_default_timeout(0)
		self.Page.context.set_default_navigation_timeout(0)
	
	def Download(self, PhotoID):
		Log.debug(f'Downloading Photo ID: {PhotoID[:10]}...')
		self.Page.goto(Config.GPhotoURL + PhotoID)
		
		with self.Page.expect_download() as Info:
			self.Page.keyboard.press('Shift+D')
		
		Download = Info.value
		FilePath = str(Config.DownloadDirectory / Download.suggested_filename)
		Download.save_as(FilePath)
		Log.info(f'Downloaded Photo ID: {PhotoID[:10]}...')
		self.Page.goto('about:blank')
		return FilePath
	
	def StartServer(self):
		Server = HTTPServer(('0.0.0.0', Config.ServerPort), lambda *args: RequestHandler(*args, app=self))
		ServerThread = threading.Thread(target=Server.serve_forever)
		ServerThread.daemon = True
		ServerThread.start()
		Log.debug(f'Server Started On Port {Config.ServerPort}')
	
	def Receive(self, PhotoID):
		Log.debug(f'Received Photo ID: {PhotoID[:10]}')
		self.Queue.put(PhotoID)

	def Close(self):
		try:
			if self.Instance:
				self.Instance.close()
			if self.PlaywrightInstance:
				self.PlaywrightInstance.stop()
		except playwright.sync_api.Error:
			Log.warning('Browser Has Been Closed')

if __name__ == '__main__':
    try:
        Log.info('Starting Browser')
        Instance = Photos(Args.headless)
        Log.info('Browser Started')
        StopEvent = threading.Event()
        try:
            while not StopEvent.is_set():
                Log.debug(f'Queue Size: {Instance.Queue.qsize()}')
                if not Instance.Queue.empty():
                    PhotoID = Instance.Queue.get()
                    FilePath = Instance.Download(PhotoID)
                    Instance.GlobalQueue.put(FilePath)
                    Instance.Queue.task_done()
                time.sleep(0.1)
        except KeyboardInterrupt:
            Log.warning('Keyboard Interrupt Detected - Force Exiting')
            os._exit(0)
    except Exception as E:
        Log.error(f'[{E.__class__.__name__}] {E}')
        os._exit(1)
    finally:
        if 'Instance' in locals():
            try:
                Instance.Close()
            except Exception:
                pass
        Log.warning('Exiting')