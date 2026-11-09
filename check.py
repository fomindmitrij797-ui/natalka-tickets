"""GitHub Actions ticket monitor with a Telegram reply-keyboard check command."""
import asyncio
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

PAGE = 'https://mediumquality.ru/natalnayakarta'
STATE = Path('state.json')
MONTHS = dict(zip('января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(), range(1, 13)))
MONTH_RE = '|'.join(MONTHS)
SOLD_OUT = ('нет билетов', 'билетов нет', 'все билеты проданы', 'билеты закончились',
            'билетов не осталось', 'продажа завершена', 'sold out', 'распродано',
            'мест нет', 'мест не осталось')
BUY_BUTTONS = ('выбрать место', 'выбрать места', 'выбрать билет', 'выбрать билеты',
               'добавить в корзину', 'оформить заказ', 'перейти к оплате')
BUTTON_CHECK = '🔎 Проверить билеты'
BUTTON_STATUS = '📊 Статус бота'
KEYBOARD = {'keyboard': [[{'text': BUTTON_CHECK}], [{'text': BUTTON_STATUS}]],
            'resize_keyboard': True, 'is_persistent': True}


def read_state():
    try:
        state = json.loads(STATE.read_text('utf-8'))
        if isinstance(state, dict):
            return state
    except (FileNotFoundError, ValueError):
        pass
    return {'initialized': False, 'links': {}, 'telegram_offset': 0}


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
        event = date(today.year + 1, month, day)
    return event if event >= today else None


def parse_announcement(html, today):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'footer']):
        tag.decompose()
    text = soup.get_text(' ', strip=True)
    start = text.upper().find('СЪЁМКИ НОВЫХ ВЫПУСКОВ')
    end = text.upper().find('БЫСТРО КОНЧИЛИСЬ', start + 1)
    if start < 0:
        raise RuntimeError('Не найден блок анонса на сайте')
    announcement = text[start:end if end > start else None]
    event = date_from_announcement(announcement, today)
    links = {}
    for a in soup.select('a[href]'):
        href = a.get('href', '').strip()
        u = urlparse(href)
        if (u.scheme == 'https' and u.hostname and
                (u.hostname == 'intickets.ru' or u.hostname.endswith('.intickets.ru'))
                and '/seance/' in u.path):
            links[href] = ' '.join(a.stripped_strings)[:130] or 'Билеты'
    return event, links


async def telegram_api(client, token, method, payload):
    response = await client.post(f'https://api.telegram.org/bot{token}/{method}', json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError(f'Telegram API method {method} returned an error')
    return result['result']


async def telegram_send(client, token, chat, message):
    return await telegram_api(client, token, 'sendMessage', {
        'chat_id': chat, 'text': message, 'disable_web_page_preview': True,
        'reply_markup': KEYBOARD
    })


async def get_commands(client, token, chat, state):
    """Read only messages from the configured chat; remember the last update id."""
    offset = int(state.get('telegram_offset', 0))
    updates = await telegram_api(client, token, 'getUpdates', {
        'offset': offset, 'timeout': 0, 'limit': 100,
        'allowed_updates': ['message']
    })
    commands = []
    for update in updates:
        offset = max(offset, int(update['update_id']) + 1)
        message = update.get('message') or {}
        if str((message.get('chat') or {}).get('id', '')) != chat:
            continue
        body = (message.get('text') or '').strip()
        if body in ('/start', BUTTON_CHECK, BUTTON_STATUS, '/check', '/status') or body.startswith(('/start@', '/check@', '/status@')):
            commands.append(body)
    print('Telegram: получено обновлений:', len(updates), '; команд из своего чата:', len(commands))
    return offset, commands


async def check_ticket(browser, url):
    tab = await browser.new_page()
    try:
        await tab.goto(url, wait_until='domcontentloaded', timeout=18000)
        await tab.wait_for_timeout(1800)
        body = ' '.join((await tab.locator('body').inner_text(timeout=7000)).lower().split())
        labels = await tab.locator('button:visible:not([disabled]),[role="button"]:visible:not([aria-disabled="true"])').all_inner_texts()
        if any(x in body for x in SOLD_OUT):
            return 'soldout'
        if any(x in label.lower() for label in labels for x in BUY_BUTTONS):
            return 'available'
        return 'unknown'
    except Exception as err:
        print('Не удалось определить наличие на странице Intickets:', type(err).__name__)
        return 'unknown'
    finally:
        await tab.close()


def render_check_result(event, results, checked_at):
    msg = f'🔎 Проверка завершена\n🕒 {checked_at:%d.%m.%Y %H:%M} UTC\n'
    if not event:
        return msg + '📅 На сайте пока не обнаружены будущие даты съёмок.\nБилеты не подтверждены.\n' + PAGE
    if not results:
        return msg + f'📅 Дата съёмки: {event:%d.%m.%Y}\nСсылки на покупку не обнаружены.\n' + PAGE
    translations = {'available': '🟢 Есть признаки доступной покупки (проверь вручную)',
                    'soldout': '🔴 Билеты отсутствуют / продажа закрыта',
                    'unknown': '⚪ Не удалось подтвердить наличие'}
    parts = [msg + f'📅 Дата съёмки: {event:%d.%m.%Y}']
    for url, label, status in results[:5]:
        parts.append(f'{label}: {translations[status]}\n{url}')
    return '\n\n'.join(parts)[:3900]


async def main():
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    chat = os.environ['TELEGRAM_CHAT_ID'].strip()
    state = read_state()
    checked_at = datetime.now(timezone.utc)
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; TicketAvailabilityMonitor/1.1)'}) as client:
        new_offset, commands = await get_commands(client, token, chat, state)
        # Always check on a scheduled run, then answer any outstanding button presses.
        try:
            html_response = await client.get(PAGE)
            html_response.raise_for_status()
            event, links = parse_announcement(html_response.text, checked_at.date())
            print('Дата мероприятия:', event or 'нет будущей даты', '; ссылок:', len(links))
            known = state.setdefault('links', {})
            results = []
            if event and links:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
                    try:
                        for url, label in links.items():
                            previous = known.get(url)
                            previous_status = previous.get('status', 'unknown') if previous else 'unknown'
                            status = await check_ticket(browser, url)
                            results.append((url, label, status))
                            print('Билет:', label, 'статус:', status)
                            if not previous:
                                await telegram_send(client, token, chat,
                                    f'🎟 Новая ссылка на «Натальную карту»!\n📅 {event:%d.%m.%Y}\n🎤 {label}\n{url}\n\nПроверь наличие билетов. Статус: ' +
                                    ('похоже, покупка доступна' if status == 'available' else 'не подтверждён'))
                            elif previous_status == 'soldout' and status == 'available':
                                await telegram_send(client, token, chat,
                                    f'🚨 Возможно, билеты ВЕРНУЛИСЬ!\n📅 {event:%d.%m.%Y}\n🎤 {label}\n{url}\n\nПроверь и покупай быстро!')
                            known[url] = {'label': label, 'date': event.isoformat(),
                                          'status': status if status != 'unknown' else previous_status}
                    finally:
                        await browser.close()
            state['initialized'] = True
            state['last_checked'] = checked_at.isoformat()
            state['last_event_date'] = event.isoformat() if event else ''
            result_text = render_check_result(event, results, checked_at)
        except Exception as exc:
            print('Ошибка проверки сайта:', type(exc).__name__, str(exc)[:300])
            if any(c in ('/check', BUTTON_CHECK) or c.startswith('/check@') for c in commands):
                await telegram_send(client, token, chat,
                    '⚠️ Проверить билеты сейчас не удалось. Повторная проверка будет при следующем запуске GitHub Actions.')
            raise

        # Reply to every button press; the previous version silently discarded all
        # but the newest press when several accumulated between scheduled runs.
        for command in commands[-10:]:
            if command.startswith('/start'):
                await telegram_send(client, token, chat,
                    '👋 Мониторинг «Натальной карты» включён.\n'
                    'Кнопки работают во время запусков GitHub Actions, не мгновенно.\n\n' + result_text)
            elif command == BUTTON_STATUS or command.startswith('/status'):
                await telegram_send(client, token, chat,
                    '✅ Мониторинг включён.\nПоследняя проверка:\n\n' + result_text)
            else:
                await telegram_send(client, token, chat, result_text)
            print('Telegram: отправлен ответ на команду:',
                  'start' if command.startswith('/start') else
                  'status' if command in (BUTTON_STATUS, '/status') else 'check')

        state['telegram_offset'] = new_offset
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Проверка завершена; обработано команд:', len(commands))


if __name__ == '__main__':
    asyncio.run(main())
