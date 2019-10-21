from http.client import responses as status_codes
from pathlib import Path
from urllib.parse import urlencode, urlparse
import logging

from bs4 import BeautifulSoup
from tornado.gen import coroutine
from tornado.httpclient import AsyncHTTPClient, HTTPError
from tornado.web import RequestHandler

from config import Session, STATIC_ROOT
from models import get_response, save_response, save_from_html, Workspace, Page
from transform_html import transformed_response_body, remove_scripts_and_proxy


class ProxyHandler(RequestHandler):
    @coroutine
    def get(self, ws_id):
        session = Session()
        ws = session.query(Workspace).get(int(ws_id))
        url = self.get_argument('url')
        referer = self.get_argument('referer', None)
        page = session.query(Page).filter_by(
            workspace=ws.id, url=referer or url).one()

        headers = self.request.headers.copy()
        for field in ['cookie', 'referer', 'host']:
            try:
                del headers[field]
            except KeyError:
                pass
        if referer:
            headers['referer'] = referer
        is_local_url = url.startswith("file://")

        session = Session()
        response = get_response(session, page, url)
        if response is None:
            if is_local_url:
                _, _, path, _, _, _ = urlparse(url)
                logging.info(f'Processing local url: {path}')
                with open(path, 'r', encoding="utf8") as file_h:
                    html = file_h.read()
                response = save_from_html(session=session, page=page, url=url, html=html)
            else:
                logging.info(f'Processing non-local url: {url}')
                httpclient = AsyncHTTPClient()
                response = yield httpclient.fetch(url, raise_error=False)
                response = save_response(
                    session, page, url, response, is_main=referer is None)
        reason = None if response.code in status_codes else 'Unknown'
        self.set_status(response.code, reason=reason)

        proxy_url_base = self.reverse_url('proxy', ws.id)

        def proxy_url(resource_url):
            return '{}?{}'.format(proxy_url_base, urlencode({
                'url': resource_url, 'referer': page.url,
            }))

        if not is_local_url:
            html_transformed, body = transformed_response_body(
                response, inject_scripts_and_proxy, proxy_url)
        else:
            html_transformed, body = transformed_response_body(
                response, inject_scripts, proxy_url)

        self.write(body)
        proxied_headers = {'content-type'}  # TODO - other?
        if response.headers:
            for k, v in response.headers.get_all():
                if k.lower() in proxied_headers:
                    if html_transformed and k.lower() == 'content-type':
                        # change encoding (always utf8 now)
                        v = 'text/html; charset=UTF-8'
                    self.set_header(k, v)
        self.finish()


def inject_scripts_and_proxy(soup: BeautifulSoup, base_url: str, proxy_url):
    remove_scripts_and_proxy(soup, base_url=base_url, proxy_url=proxy_url)
    inject_scripts(soup=soup, base_url=base_url, proxy_url=proxy_url)


def inject_scripts(soup: BeautifulSoup, base_url: str, proxy_url):
    # parameters only to keep function signature compatible
    del base_url
    del proxy_url

    body = soup.find('body')
    if not body:
        return

    js_tag = soup.new_tag('script', type='text/javascript')
    injected_js = (Path(STATIC_ROOT) / 'js' / 'injected.js').read_text('utf8')
    js_tag.string = injected_js
    body.append(js_tag)

    css_tag = soup.new_tag('style')
    injected_css = (
        Path(STATIC_ROOT) / 'css' / 'injected.css').read_text('utf8')
    css_tag.string = injected_css
    # TODO - create "head" if none exists
    soup.find('head').append(css_tag)
