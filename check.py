"""One GitHub Actions run: check show page, check ticket status, notify, persist state."""
import asyncio
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

PAGE = 'https://mediumquality.ru/natalnayakarta'
STATE = Path('state.json')
MONTHS = dict(zip('января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(),range(1,13)))
MONTH_RE = '|'.join(MONTHS)
SOLD_OUT = ('нет билетов','билетов нет','все билеты проданы','билеты закончились','билетов не осталось','продажа завершена','sold out','распродано','мест нет','мест не осталось')
BUY_BUTTONS = ('выбрать место','выбрать места','выбрать билет','выбрать билеты','добавить в корзину','оформить заказ','перейти к оплате')


def read_state():
    try:
        state = json.loads(STATE.read_text('utf-8'))
        if isinstance(state, dict):
            return state
    except (FileNotFoundError, ValueError):
        pass
    return {'initialized': False, 'links': {}}


def date_from_announcement(text, today):
    match = re.search(r'\b(\d{1,2})\s+(' + MONTH_RE + r')\b(?:\s+(20\d{2}))?', text.lower())
    if not match:
        return None
    day, month = int(match[1]), MONTHS[match[2]]
    year = int(match[3]) if match[3] else today.year
    try:
        event = date(year, month, day)
    except ValueError:
        return None
    if event < today and not match[3] and today.month >= 11 and month <= 2:
        event = date(today.year+1, month, day)
    return event if event >= today else None


def parse_announcement(html, today):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script','style','noscript','footer']):
        tag.decompose()
    text = soup.get_text(' ', strip=True)
    start = text.upper().find('СЪЁМКИ НОВЫХ ВЫПУСКОВ')
    end = text.upper().find('БЫСТРО КОНЧИЛИСЬ', start + 1)
    if start < 0:
        raise RuntimeError('Не найден блок анонса на сайте')
    announcement = text[start:end if end > start else None]
    event = date_from_announcement(announcement,today)
    links = {}
    for a in soup.select('a[href]'):
        href = a.get('href','').strip()
        u = urlparse(href)
        if u.scheme == 'https' and u.hostname and u.hostname.endswith('.intickets.ru') and '/seance/' in u.path:
            links[href] = ' '.join(a.stripped_strings)[:130] or 'Билеты'
    return event, links


async def telegram_send(client, token, chat, message):
    response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={
        'chat_id': chat,'text': message,'disable_web_page_preview': True
    },timeout=20)
    if response.status_code != 200 or not response.json().get('ok'):
        raise RuntimeError('Не удалось отправить Telegram-сообщение; проверь Secrets и /start')


async def check_ticket(browser, url):
    tab = await browser.new_page()
    try:
        await tab.goto(url,wait_until='domcontentloaded',timeout=18000)
        await tab.wait_for_timeout(1800)
        body = ' '.join((await tab.locator('body').inner_text(timeout=7000)).lower().split())
        labels = await tab.locator('button:visible:not([disabled]),[role="button"]:visible:not([aria-disabled="true"])').all_inner_texts()
        if any(x in body for x in SOLD_OUT):
            return 'soldout'
        if any(x in label.lower() for label in labels for x in BUY_BUTTONS):
            return 'available'
        return 'unknown'
    except Exception as err:
        print('Не удалось определить наличие на странице Intickets:',type(err).__name__)
        return 'unknown'
    finally:
        await tab.close()


async def main():
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    chat = os.environ['TELEGRAM_CHAT_ID'].strip()
    state = read_state()
    async with httpx.AsyncClient(follow_redirects=True,timeout=25,headers={'User-Agent':'Mozilla/5.0 (compatible; TicketAvailabilityMonitor/1.0)'}) as client:
        html_response = await client.get(PAGE)
        html_response.raise_for_status()
        event, links = parse_announcement(html_response.text,datetime.now().astimezone().date())
        print('Дата мероприятия:',event or 'нет будущей даты','; ссылок:',len(links))
        known = state.setdefault('links',{})
        if event:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True,args=['--no-sandbox'])
                try:
                    for url,label in links.items():
                        previous = known.get(url)
                        previous_status = previous.get('status','unknown') if previous else 'unknown'
                        status = await check_ticket(browser,url)
                        print('Билет:',label,'статус:',status)
                        if not previous:
                            # A new announcement is worth a warning even if ticket stock is uncertain.
                            await telegram_send(client,token,chat,f'🎟 Новая ссылка на «Натальную карту»!\n📅 {event:%d.%m.%Y}\n🎤 {label}\n{url}\n\nПроверь наличие билетов. Статус: {"похоже, покупка доступна" if status == "available" else "не подтверждён"}.')
                        elif previous_status == 'soldout' and status == 'available':
                            await telegram_send(client,token,chat,f'🚨 Возможно, билеты ВЕРНУЛИСЬ!\n📅 {event:%d.%m.%Y}\n🎤 {label}\n{url}\n\nПроверь и покупай быстро!')
                        known[url] = {'label':label,'date':event.isoformat(),'status':status if status != 'unknown' else previous_status}
                finally:
                    await browser.close()
        state['initialized'] = True
        state['last_checked'] = datetime.now().astimezone().isoformat()
        state['last_event_date'] = event.isoformat() if event else ''
        STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print('Проверка завершена')


if __name__ == '__main__':
    asyncio.run(main())
