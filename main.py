        if debug or screenshot:
            await save_debug(store, page, url, log, screenshot)
        body_text = (await page.locator('body').inner_text(timeout=10000))[:12000]
        blocked = bool(re.search(r'captcha|verify you are human|access denied|unusual traffic', body_text, re.I))
        description = await page.locator('meta[name="description"]').get_attribute('content') if await page.locator('meta[name="description"]').count() else None
        record = {
            'sourceUrl': url, 'finalUrl': final_url, 'httpStatus': status,
            'title': title, 'description': description, 'region': region,
            'blockedOrChallengeDetected': blocked,
            'extractionStatus': 'page_loaded' if status and status < 400 and not blocked else 'needs_review',
        }
        await Actor.push_data(record)
        log.info('[PRODUCT] Saved 1 diagnostic record to dataset')
    except Exception as exc:
        log.exception(f'[PRODUCT] Failed {url}: {type(exc).__name__}: {exc}')
        await Actor.push_data({
            'sourceUrl': url, 'region': region, 'extractionStatus': 'error',
            'error': f'{type(exc).__name__}: {exc}'
        })
    finally:
        await context.close()


async def main():
    async with Actor:
        log = Actor.log
        log.info('[START] main.py started')
        data = await Actor.get_input() or {}
        log.info(f'[INPUT] Fields: {sorted(data.keys())}')
        urls = normalize_urls(data.get('productUrls') or data.get('startUrls') or data.get('start_urls'))
        urls += [u for u in normalize_urls(data.get('categoryUrls')) if u not in urls]
        if not urls:
            log.warning('[INPUT] No valid productUrls/startUrls/categoryUrls. Nothing to scrape.')
            return
        log.info(f'[INPUT] URLs queued: {len(urls)}')
        region = str(data.get('region') or 'GB')
        language = str(data.get('acceptLanguage') or 'en-GB').split(',')[0].split(';')[0].strip()
        if not re.fullmatch(r'[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?', language):
            language = 'en-GB'
        raw_timeouts = data.get('timeouts') or {}
        timeout_ms = int(raw_timeouts.get('navigationTimeoutSecs', 30)) * 1000 if isinstance(raw_timeouts, dict) else 30000
        timeout_ms = max(5000, min(timeout_ms, 120000))
        debug = bool(data.get('debug', False))
        screenshot = bool(data.get('captureScreenshots', False))
        store = await Actor.open_key_value_store()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                # Sequential first, to make debugging and per-product logs deterministic.
                for url in urls:
                    await scrape_one(browser, url, region, language, timeout_ms, store, debug, screenshot)
            finally:
                await browser.close()
        log.info('[DONE] All URLs processed')


if __name__ == '__main__':
    asyncio.run(main())
