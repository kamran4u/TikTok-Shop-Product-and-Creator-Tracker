#!/usr/bin/env python3
"""TikTok Shop page diagnostics and public-page metadata collector for Apify."""
import asyncio
import hashlib
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from apify import Actor
from playwright.async_api import async_playwright, Browser, Page

PRODUCT_LABEL = 'PRODUCT'
LISTING_LABEL = 'LISTING'
DEFAULT_NAVIGATION_TIMEOUT_MS = 30000


def safe_key(prefix: str, url: str, suffix: str) -> str:
    return f'{prefix}-{hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]}{suffix}'


def normalize_start_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, (str, dict)):
        raw = [raw]
    result = []
    for item in raw or []:
        if isinstance(item, str):
            result.append({'url': item, 'userData': {}})
        elif isinstance(item, dict) and item.get('url'):
            data = dict(item.get('userData') or {})
            if item.get('label'):
                data['label'] = item['label']
            result.append({'url': item['url'], 'userData': data})
    return result


def choose_label(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    if any(x in path for x in ('/tag/', '/collections/', '/category/', '/search')):
        return LISTING_LABEL
    return PRODUCT_LABEL


def navigation_timeout(input_data: dict) -> int:
    t = input_data.get('timeouts') or {}
    try:
        if 'navigation' in t:
            return max(1000, int(t['navigation']))
        if 'navigationTimeoutSecs' in t:
            return max(1000, int(t['navigationTimeoutSecs']) * 1000)
    except (TypeError, ValueError):
        pass
    return DEFAULT_NAVIGATION_TIMEOUT_MS


def get_locale(raw: str) -> str:
    first = (raw or 'en-GB').split(',')[0].split(';')[0].strip().replace('_', '-')
    return first if re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?', first) else 'en-GB'


async def save_debug(page: Page, store, url: str, screenshot: bool, log) -> None:
    try:
        html = await page.content()
        key = safe_key('debug-page', url, '.html')
        await store.set_value(key, html, content_type='text/html')
        log.info(f'[DEBUG] Saved {key}')
    except Exception as exc:
        log.warning(f'[DEBUG] Could not save HTML: {exc}')
    if screenshot:
        try:
            png = await page.screenshot(full_page=True, timeout=15000)
            key = safe_key('screenshot', url, '.png')
            await store.set_value(key, png, content_type='image/png')
            log.info(f'[SCREENSHOT] Saved {key}')
        except Exception as exc:
            log.warning(f'[SCREENSHOT] Could not save image: {exc}')


async def visit(browser: Browser, request: dict, config: dict, queue, store, log) -> None:
    url = request['url']
    label = (request.get('userData') or {}).get('label') or choose_label(url)
    context = None
    record = {'url': url, 'region': config['region'], 'label': label}
    try:
        context = await browser.new_context(
            locale=config['locale'],
            viewport={'width': 1280, 'height': 800},
        )
        page = await context.new_page()
        response = await page.goto(url, wait_until='domcontentloaded', timeout=config['timeout'])
        await page.wait_for_timeout(1800)
        title = await page.title()
        body = (await page.locator('body').inner_text(timeout=10000))[:10000]
        final_url = page.url
        status = response.status if response else None
        blocked = bool(re.search(r'security check|captcha|verify you are human|unusual traffic|access denied', title + ' ' + body[:1500], re.I))
        region_mismatch = 'region_not_match' in final_url
        record.update({
            'finalUrl': final_url,
            'title': title,
            'httpStatus': status,
            'blockedOrChallengeDetected': blocked,
            'regionMismatchDetected': region_mismatch,
            'extractionStatus': 'security_challenge' if blocked else ('region_mismatch' if region_mismatch else 'page_loaded'),
        })
        log.info(f'[{label}] HTTP={status} title={title!r} blocked={blocked} regionMismatch={region_mismatch}')
        if config['debug']:
            await save_debug(page, store, url, config['screenshots'], log)
        elif config['screenshots']:
            try:
                png = await page.screenshot(full_page=True, timeout=15000)
                await store.set_value(safe_key('screenshot', url, '.png'), png, content_type='image/png')
            except Exception as exc:
                log.warning(f'[SCREENSHOT] {exc}')
        if label == LISTING_LABEL and not blocked and not region_mismatch:
            for _ in range(4):
                await page.evaluate('window.scrollBy(0, 1100)')
                await page.wait_for_timeout(500)
            hrefs = await page.locator('a[href]').evaluate_all('(els) => els.map(a => a.href)')
            candidates = list(dict.fromkeys(
                href for href in hrefs
                if urllib.parse.urlparse(href).hostname in ('www.tiktok.com', 'shop.tiktok.com')
                and any(x in urllib.parse.urlparse(href).path.lower() for x in ('/pdp/', '/product/', '/item/'))
            ))[:config['max_links']]
            for href in candidates:
                await queue.add_request({'url': href, 'userData': {'label': PRODUCT_LABEL}})
            record['discoveredProductLinks'] = len(candidates)
        if label == PRODUCT_LABEL and not blocked and not region_mismatch:
            description = await page.locator('meta[name="description"]').get_attribute('content') if await page.locator('meta[name="description"]').count() else None
            record['description'] = description
        # HTTP 200 can still be a security challenge. Do not claim successful extraction.
    except Exception as exc:
        record['extractionStatus'] = 'error'
        record['error'] = f'{type(exc).__name__}: {exc}'
        log.error(f'[{label}] {url}: {record["error"]}')
    finally:
        await Actor.push_data(record)
        if context:
            await context.close()


async def worker(browser, queue, config, store, log, worker_id):
    log.info(f'Worker {worker_id} started')
    while True:
        request = await queue.fetch_next_request()
        if request is None:
            return
        try:
            await visit(browser, request, config, queue, store, log)
        finally:
            await queue.mark_request_as_handled(request)


async def main():
    async with Actor:
        log = Actor.log
        data = await Actor.get_input() or {}
        raw = data.get('startUrls') or data.get('start_urls') or data.get('productUrls') or []
        items = normalize_start_items(raw)
        keywords = data.get('keywords') or data.get('keyword') or data.get('searchKeywords') or []
        if isinstance(keywords, str):
            keywords = [keywords]
        template = data.get('searchUrlTemplate') or 'https://www.tiktok.com/tag/{keyword}'
        for keyword in keywords:
            items.append({'url': template.format(keyword=urllib.parse.quote_plus(str(keyword))), 'userData': {'label': LISTING_LABEL}})
        for category in data.get('tiktokCategories') or []:
            slug = re.sub(r'[^a-z0-9]+', '-', str(category).lower().replace('&', 'and')).strip('-')
            items.append({'url': f'https://www.tiktok.com/tag/{slug}', 'userData': {'label': LISTING_LABEL}})
        for url in data.get('categoryUrls') or []:
            items.append({'url': url, 'userData': {'label': LISTING_LABEL}})
        if not items:
            log.warning('No URLs, keywords, or categories provided')
            return
        config = {
            'locale': get_locale(data.get('acceptLanguage', 'en-GB')),
            'region': data.get('region', 'GB'),
            'timeout': navigation_timeout(data),
            'debug': bool(data.get('debug', False)),
            'screenshots': bool(data.get('captureScreenshots', False)),
            'max_links': max(1, min(100, int(data.get('maxDiscoveredLinks', 30)))),
        }
        if data.get('useProxy'):
            log.warning('useProxy requested, but this diagnostic version does not configure a proxy. Running without one.')
        queue = await Actor.open_request_queue()
        store = await Actor.open_key_value_store()
        for item in items:
            url = item['url']
            if urllib.parse.urlparse(url).scheme not in ('https', 'http'):
                log.warning(f'Skipping invalid URL: {url}')
                continue
            user_data = item.get('userData') or {}
            user_data['label'] = user_data.get('label') or choose_label(url)
            await queue.add_request({'url': url, 'userData': user_data})
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                concurrency = max(1, min(5, int(data.get('maxConcurrency', 3))))
                await asyncio.gather(*(worker(browser, queue, config, store, log, i) for i in range(concurrency)))
            finally:
                await browser.close()
        log.info('Run finished')


if __name__ == '__main__':
    asyncio.run(main())
