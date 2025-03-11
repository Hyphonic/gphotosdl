# Web Scraping
from browserforge.fingerprints import Screen
import playwright, camoufox

# Standard Library
import threading, argparse, asyncio, queue, time, os, logging, socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Logging
import playwright.async_api
from rich.console import Console as RichConsole
from rich.traceback import install as Install
from rich.highlighter import RegexHighlighter
from rich.logging import RichHandler
from rich.theme import Theme

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
	BaseURL, LoginURL, PhotoURL = 'https://photos.google.com/', 'https://photos.google.com/login', 'https://photos.google.com/lr/photo/'
	Profile, DownloadDirectory = Path.home() / '.gphotosdl' / 'profile', Path.home() / '.gphotosdl' / 'downloads'
	ServerPort, ReadChunkSize = Args.port, int(1e6)
	PhotoIDTrim, FileNameTrim = 40, 40

	@classmethod
	def InitializeFolders(cls):
		Log.debug('Setting Up Directories')
		for Directory in [cls.Profile.parent, cls.Profile, cls.DownloadDirectory]:
			Directory.mkdir(parents=True, exist_ok=True, mode=0o755)
		if cls.Profile.exists():
			Log.debug(f'Profile Directory Size: {HumanizeBytes(sum(f.stat().st_size for f in cls.Profile.rglob("*") if f.is_file()))}')

# Logging Setup
class DownloadHighlighter(RegexHighlighter):
	base_style = 'browser.'
	highlights = [r'(?P<time>[\d.]+s)', r'\[(?P<error>error|Error)\]', r'\[(?P<warning>warning|Warning)\]',
					r'\[(?P<info>info|Info)\]', r'\[(?P<debug>debug|Debug)\]', r'(?P<size>\d+\.\d+ [KMGT]B)',
					fr'(?P<hash>[a-zA-Z0-9-_]{{{Config.PhotoIDTrim}}})', r'\[(?P<speed>\d+\.\d+ [KMGT]B/s)\]',
					r'(?:Downloaded |Uploaded And Deleted )(?P<filename>[^\(\s]+)(?=\s|\.{3}|\(|$)']

ThemeDict = {**{f'logging.level.{lvl}': col for lvl, col in zip(['debug', 'info', 'warning', 'error'],
			['#B3D7EC', '#A0D6B4', '#F5D7A3', '#F5A3A3'])},
			**{f'browser.{key}': val for key, val in {'time': '#F5D7A3', 'error': '#F5A3A3', 'warning': '#F5D7A3',
			'info': '#A0D6B4', 'debug': '#B3D7EC', 'size': '#A0D6B4', 'hash': '#B3D7EC', 'speed': '#D8BFD8',
			'filename': '#DDA0DD'}.items()},
			'log.time': 'bright_black'}

# Initialize logging
Console = RichConsole(theme=Theme(ThemeDict), force_terminal=True, log_path=False, highlighter=DownloadHighlighter(), color_system='truecolor')
ConsoleHandler = RichHandler(markup=True, rich_tracebacks=True, show_time=True, console=Console, show_path=False, omit_repeated_times=True, highlighter=DownloadHighlighter())
ConsoleHandler.setFormatter(logging.Formatter('%(message)s', datefmt='[%H:%M:%S]'))
logging.basicConfig(handlers=[ConsoleHandler], force=True, level=logging.DEBUG if Args.debug else logging.INFO)
Log = logging.getLogger('rich')
Log.handlers.clear(), Log.addHandler(ConsoleHandler), setattr(Log, 'propagate', False)
Install(show_locals=True, console=Console, max_frames=1)

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
			try:
				self.send_response(200), self.send_header('Content-Type', 'text/html'), self.end_headers()
				self.wfile.write(b'<h1>Google Photos Downloader</h1>')
			except (ConnectionResetError, BrokenPipeError): 
				return
		elif self.path.startswith('/id/'):
			PhotoID, ResponseQueue, FilePath = self.path[4:], queue.Queue(), None
			try:
				asyncio.run_coroutine_threadsafe(self.App.Receive(PhotoID, ResponseQueue), self.App.EventLoop)
				FilePath = ResponseQueue.get(timeout=1800)

				if FilePath is None or not os.path.exists(FilePath):
					Log.error(f'File Missing: {FilePath}')
					self.send_response(404), self.end_headers()
					return

				SafeFilename = os.path.basename(FilePath).encode('ascii', 'ignore').decode('ascii') or f'Photo_{PhotoID[-Config.PhotoIDTrim:]}'
				FileSize = os.path.getsize(FilePath)

				try:
					self.send_response(200)
					self.send_header('Content-Type', 'application/octet-stream')
					self.send_header('Content-Disposition', f'attachment; filename="{SafeFilename}"')
					self.send_header('Content-Length', str(FileSize))
					self.end_headers()

					with open(FilePath, 'rb') as File:
						try:
							while Chunk := File.read(Config.ReadChunkSize):
								self.wfile.write(Chunk)
						except (ConnectionResetError, BrokenPipeError):
							Log.warning(f'Connection Closed During Download Of {SafeFilename}')
							return
				except (ConnectionResetError, BrokenPipeError):
					Log.warning('Connection Closed Before Download Started')
					return
				finally:
					if FilePath and os.path.exists(FilePath):
						try: 
							os.remove(FilePath)
						except OSError as E: 
							Log.warning(f'Failed To Delete {FilePath}: {E}')
					Log.info(f'╰ Uploaded And Deleted {SafeFilename[:Config.FileNameTrim]}{"..." if len(SafeFilename) > Config.FileNameTrim else ""}')
			except queue.Empty:
				try: 
					self.send_response(504), self.end_headers()
				except (ConnectionResetError, BrokenPipeError): 
					return
			except Exception as E:
				try:
					Log.error(f'Download Failed: {E}')
					self.send_response(500), self.end_headers()
				except (ConnectionResetError, BrokenPipeError): 
					return

	def log_message(self, format, *args): Log.debug(f'{self.address_string()} - {format % args}')

# Browser
class Photos:
	def __init__(self, Headless=False):
		self.EventLoop = asyncio.new_event_loop()
		self.Queue, self.GlobalQueue, self.PagePool, self.PageLock = asyncio.Queue(), queue.Queue(), [], asyncio.Lock()
		self.MaxConcurrent = Args.threads
		self.ConfigInfo = {'Headless Mode': Args.headless, 'Debug Mode': Args.debug,
						  'Server Port': Config.ServerPort, 'Allow Images': Args.allow_images,
						  'Concurrent Downloads': self.MaxConcurrent}

		self.EventLoop.create_task(self.Initialize(Headless))
		self.LoopThread = threading.Thread(target=lambda: [asyncio.set_event_loop(self.EventLoop), self.EventLoop.run_forever()])
		self.LoopThread.daemon = True
		self.LoopThread.start()

		while not hasattr(self, 'IsInitialized') or not self.IsInitialized: 
			time.sleep(0.1)

	async def Initialize(self, Headless):
		Log.info('Google Photos Downloader Configuration:')
		for Key, Value in self.ConfigInfo.items(): 
			Log.info(f'• {Key}: {Value}')

		self.PlaywrightInstance = await playwright.async_api.async_playwright().start()
		self.Instance = await camoufox.AsyncNewBrowser(
			playwright=self.PlaywrightInstance,
			from_options=camoufox.launch_options(
				screen=Screen(min_width=1279, min_height=719, max_width=1280, max_height=720),
				window=[1280, 720], os=['windows', 'linux'],
				user_data_dir=str(Config.Profile), color_scheme='dark', headless=Headless,
				java_script_enabled=True, i_know_what_im_doing=True, block_images=Args.allow_images
			), persistent_context=True
		)

		self.Page = self.Instance.pages[0]
		self.Page.context.set_default_timeout(0)
		self.Page.context.set_default_navigation_timeout(0)

		Log.info('Please Login To Google Photos')
		await self.Page.goto(Config.LoginURL)
		while not self.Page.url.startswith(Config.BaseURL): 
			await asyncio.sleep(1)

		Log.info('Logged In')
		await self.Page.goto('about:blank')
		self.PagePool.append(self.Page)

		for i in range(1, self.MaxConcurrent):
			page = await self.Instance.new_page()
			page.context.set_default_timeout(0)
			page.context.set_default_navigation_timeout(0)
			self.PagePool.append(page)

		Log.info(f'Created {self.MaxConcurrent} Browser Pages')

		try:
			Server = HTTPServer(('', Config.ServerPort), lambda *args: RequestHandler(*args, app=self))
			threading.Thread(target=Server.serve_forever, daemon=True).start()
			Log.info(f'Server Started On Port {Config.ServerPort}')
			for Interface in (sorted(set([socket.gethostbyname(socket.gethostname())] +
				([i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None) if i[0] == socket.AF_INET] if Args.debug else []))) or ['127.0.0.1']):
				Log.info(f'• Server Available At: http://{Interface}:{Config.ServerPort}')
		except OSError as E:
			Log.error(f'Failed To Start Server: {E}')
			os._exit(1)

		self.ProcessorTask = self.EventLoop.create_task(self.ProcessQueue())
		self.IsInitialized = True
		Log.info('Ready To Receive Requests, Start Your Rclone! 🚀')

	async def Download(self, Page, PhotoID):
		Log.info(f'╭ Downloading Photo ID: ...{PhotoID[-Config.PhotoIDTrim:]}')
		StartTime = time.time()
		await Page.goto(Config.PhotoURL + PhotoID)

		try:
			async with Page.expect_download(timeout=600000) as Info:
				await Page.keyboard.press('Shift+D')

			Download = await Info.value
			SafeFilename = Download.suggested_filename.encode('ascii', 'ignore').decode('ascii') or f'photo_{PhotoID[-Config.PhotoIDTrim:]}.jpg'
			FilePath = str(Config.DownloadDirectory / SafeFilename)
			await Download.save_as(FilePath)

			FileSize = os.path.getsize(FilePath)
			DownloadTime = time.time() - StartTime
			Log.info(f'├ Downloaded {SafeFilename[:Config.FileNameTrim]}{"..." if len(SafeFilename) > Config.FileNameTrim else ""} ({HumanizeBytes(FileSize)}) In {DownloadTime:.2f}s [{HumanizeBytes(FileSize / DownloadTime)}/s]')
			Log.debug(f'├ File Saved To {FilePath}')

			await Page.goto('about:blank')
			return FilePath
		except Exception as E:
			Log.error(f'Download error: {E}')
			await Page.goto('about:blank')
			raise

	async def Receive(self, PhotoID, ResponseQueue=None):
		Log.debug(f'Received Photo ID: ...{PhotoID[-Config.PhotoIDTrim:]}')
		await self.Queue.put((PhotoID, ResponseQueue))

	async def ProcessQueue(self):
		Tasks = set()
		while True:
			try:
				if not self.Queue.empty() and len(self.PagePool) > 0:
					Task = asyncio.create_task(self.ProcessDownload(await self.Queue.get()))
					Tasks.add(Task)
					Task.add_done_callback(Tasks.discard)
				await asyncio.sleep(0.1)
			except playwright.async_api.BrowserError:
				Log.error('Browser Connection Lost')
				break
			except Exception as E:
				Log.error(f'Queue Error: {E}')

	async def ProcessDownload(self, PhotoData):
		PhotoID, ResponseQueue = PhotoData if isinstance(PhotoData, tuple) else (PhotoData, None)
		async with self.PageLock:
			if not self.PagePool:
				await self.Queue.put((PhotoID, ResponseQueue))
				return
			Page = self.PagePool.pop()

		try:
			FilePath = await self.Download(Page, PhotoID)
			if ResponseQueue: 
				ResponseQueue.put(FilePath)
			else: 
				self.GlobalQueue.put(FilePath)
		except Exception as E:
			Log.error(f'Download error for ...{PhotoID[-Config.PhotoIDTrim:]}: {E}')
			if ResponseQueue: 
				ResponseQueue.put_nowait(None)
		finally:
			async with self.PageLock:
				self.PagePool.append(Page)

	async def Close(self):
		try:
			if hasattr(self, 'Instance'): 
				await self.Instance.close()
			if hasattr(self, 'PlaywrightInstance'): 
				await self.PlaywrightInstance.stop()
		except playwright.async_api.Error as E:
			Log.error(f'Error Closing Browser: {E}')

	def Shutdown(self):
		if hasattr(self, 'ProcessorTask'): 
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
		Instance = Photos(Args.headless)
		try:
			while True: 
				time.sleep(0.5)
		except KeyboardInterrupt:
			Log.warning('Keyboard Interrupt Detected')
		finally:
			if Instance: 
				Instance.Shutdown()
			try:
				for File in Config.DownloadDirectory.iterdir():
					if File.is_file(): 
						File.unlink()
				Config.DownloadDirectory.rmdir()
			except Exception as E:
				if Args.debug: 
					Log.warning(f'Cleanup Error: {E}')
	except Exception as E:
		Log.error(f'[{E.__class__.__name__}] {E}')
		if Instance: 
			Instance.Shutdown()
		raise