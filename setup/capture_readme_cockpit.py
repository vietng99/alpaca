#!/usr/bin/env python3
"""Capture the shipped cockpit with fictional data, without starting a server.

Optional tools: pip install playwright Pillow; playwright install chromium ffmpeg
Uses Chrome when available. Outputs PNG, GIF and WebM to docs/assets.
All browser requests are fulfilled locally; no live project record is read.
"""
from io import BytesIO
from pathlib import Path
import json
import mimetypes
import shutil
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'docs/assets'
WEB = ROOT / 'alpaca/web'
STATE = ROOT / '.alpaca/cockpit-capture'
ORIGIN = 'http://alpaca-demo.test'
FIXED_TIME = '2026-01-15T12:00:00Z'


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads((ROOT / 'setup/readme_cockpit.json').read_text())
    frames, errors, unknown = [], [], []
    # Presentation-only colors; application structure and behavior are unchanged.
    css = ''':root[data-theme="dark"]{--green:#a5c9ff;--green-bg:#203651;--accent:#79b8ff;--accent-soft:#1b2b44}
    .topbar::after{content:"DEMO / EXAMPLE DATA";position:absolute;left:48%;font:10px var(--mono);color:var(--accent);letter-spacing:1px}
    *{scroll-behavior:auto!important}'''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=shutil.which('google-chrome') or None, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width':1280,'height':800}, device_scale_factor=1,
            color_scheme='dark', locale='en-US', timezone_id='UTC',
            record_video_dir=str(STATE), record_video_size={'width':1280,'height':800})
        context.add_init_script('''localStorage.setItem('alpaca.theme','dark');
          const NativeDate=Date; const fixed=NativeDate.parse('''+json.dumps(FIXED_TIME)+''');
          window.Date=class extends NativeDate{constructor(...args){super(...(args.length?args:[fixed]));}static now(){return fixed;}};
        ''')

        def serve(route):
            url=urlparse(route.request.url)
            if url.netloc != 'alpaca-demo.test':
                unknown.append(route.request.url); route.abort(); return
            path=url.path
            if path in fixtures:
                route.fulfill(json=fixtures[path]); return
            if path == '/events':
                route.fulfill(content_type='text/event-stream',body=': demo\n\n',headers={'Cache-Control':'no-cache'}); return
            if path == '/favicon.ico':
                route.fulfill(status=204); return
            file=WEB/'hub.html' if path=='/hub/' else WEB/path.removeprefix('/hub/assets/')
            if path.startswith('/hub/assets/') or path=='/hub/':
                if file.is_file() and file.resolve().is_relative_to(WEB.resolve()):
                    if path=='/hub/':
                        route.fulfill(content_type='text/html',body=file.read_text().replace('</head>','<style>'+css+'</style></head>')); return
                    route.fulfill(path=str(file),content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream'); return
            unknown.append(path); route.fulfill(status=404,body='Not part of the demo')

        context.route('**/*',serve)
        page=context.new_page(); page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(ORIGIN+'/hub/#cockpit',wait_until='networkidle')
        page.locator('.live-session').nth(1).wait_for()
        page.evaluate('document.fonts.ready')

        def capture(name):
            page.wait_for_timeout(350)
            assert not page.locator('.error-box').count(),page.locator('main').inner_text()
            assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--green').trim()")=="#a5c9ff"
            shot=page.screenshot()
            (STATE/(name+'.png')).write_bytes(shot)
            frames.append(Image.open(BytesIO(shot)).convert('RGB'))
            page.wait_for_timeout(2600)

        capture('cockpit')
        (ASSETS/'cockpit-demo.png').write_bytes((STATE/'cockpit.png').read_bytes())
        page.locator('#navigation a[href="#work"]').click()
        page.locator('.work-view-toggle a',has_text='Cards').click()
        page.locator('.work-card').first.wait_for()
        capture('work')
        page.locator('button[data-task="t-005"]').first.click()
        page.locator('#inspector[open]').wait_for()
        page.locator('#inspector-content').evaluate('(e)=>e.scrollTop=0')
        capture('contract')
        page.locator('#inspector .detail-section',has=page.locator('h3',has_text='Result')).scroll_into_view_if_needed()
        capture('proof')
        page.locator('[data-close="inspector"]').click()
        page.locator('#navigation a[href="#cockpit"]').click()
        page.locator('.live-session').nth(1).wait_for()
        capture('return')
        assert not errors,errors
        assert not unknown,unknown
        video=page.video
        context.close()
        video.save_as(str(ASSETS/'cockpit-demo.webm'))
        browser.close()
    # Each GIF frame is an actual browser capture, with a shared palette.
    contact=Image.new('RGB',(320*len(frames),200))
    for i,f in enumerate(frames):contact.paste(f.resize((320,200)),(320*i,0))
    palette=contact.quantize(colors=256)
    indexed=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]
    indexed[0].save(ASSETS/'cockpit-demo.gif',save_all=True,append_images=indexed[1:],duration=[3000]*len(frames),loop=0,optimize=True)
    checks={'scenes':len(frames),'page_errors':errors,'unhandled_requests':unknown,'source':'Shipped hub HTML, JS and CSS with synthetic fixture responses','palette':'Blue presentation override; production styles unchanged'}
    (STATE/'checks.json').write_text(json.dumps(checks,indent=2)+'\n')
    print(json.dumps(checks,indent=2))


if __name__=='__main__':
    main()
