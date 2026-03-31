#!/usr/bin/env python3
"""
Monitor de preços do Tauá Resort Atibaia / SP.
Verifica diárias e envia notificação WhatsApp via CallMeBot quando encontrar promoções.

Thresholds:
  - Dias de semana (Seg-Sex): abaixo de R$ 1.850
  - Finais de semana (Sáb-Dom): abaixo de R$ 2.200
"""

import asyncio
import re
import os
import sys
import urllib.parse
from datetime import date, datetime
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

PHONE = "5511990178989"
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
HOTEL_URL = "https://tauaresorts.com.br/atibaia"

WEEKDAY_MAX = 1850.0
WEEKEND_MAX = 2200.0
EXTRA_MONTH_PAGES = 1

MONTHS_PT: dict[str, int] = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

def is_weekend(year: int, month: int, day: int) -> bool:
    try:
        return date(year, month, day).weekday() >= 5
    except ValueError:
        return False

def parse_brl(text: str) -> Optional[float]:
    m = re.search(r"[\d.]+,\d{2}", text)
    if m:
        try:
            return float(m.group().replace(".", "").replace(",", "."))
        except ValueError:
            pass
    return None

async def send_whatsapp(msg: str) -> None:
    if not CALLMEBOT_APIKEY:
        print(f"[SEM APIKEY] Mensagem que seria enviada:\n{msg}")
        return
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={PHONE}"
        f"&text={urllib.parse.quote(msg)}"
        f"&apikey={CALLMEBOT_APIKEY}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url)
            print(f"WhatsApp enviado → HTTP {resp.status_code}")
        except Exception as exc:
            print(f"Erro ao enviar WhatsApp: {exc}")

EXTRACT_JS = """
() => {
    const monthRe = /(Janeiro|Fevereiro|Mar[çc]o|Abril|Maio|Junho|Julho|Agosto|Setembro|Outubro|Novembro|Dezembro)\\s+(\\d{4})/i;

    function parsePrice(text) {
        const m = text.match(/R\\$\\s*([\\d.,]+)/);
        return m ? m[1] : null;
    }

    function findDayCells(root) {
        const cells = [];
        root.querySelectorAll('*').forEach(el => {
            if (el.children.length > 8) return;
            const text = (el.innerText || '').trim();
            const lines = text.split(/[\\n\\r]+/).map(s => s.trim()).filter(Boolean);
            if (lines.length < 2) return;
            const dayNum = parseInt(lines[0], 10);
            if (isNaN(dayNum) || dayNum < 1 || dayNum > 31) return;
            const price = parsePrice(text);
            if (!price) return;
            const bbox = el.getBoundingClientRect();
            if (bbox.width > 200) return;
            cells.push({ day: dayNum, price, x: bbox.left, y: bbox.top });
        });
        return cells;
    }

    const headers = [];
    document.querySelectorAll('*').forEach(el => {
        if (el.children.length > 3) return;
        const text = (el.innerText || '').trim();
        if (monthRe.test(text) && text.length < 30) headers.push(el);
    });

    if (headers.length === 0) return { error: 'Nenhum header de mês encontrado' };

    const result = [];
    const visited = new WeakSet();

    for (const header of headers) {
        const match = (header.innerText || '').match(monthRe);
        if (!match) continue;
        const monthName = match[1].toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
        const year = parseInt(match[2], 10);

        let container = header;
        let cells = [];
        for (let depth = 0; depth < 10; depth++) {
            container = container.parentElement;
            if (!container) break;
            if (visited.has(container)) break;
            cells = findDayCells(container);
            if (cells.length >= 10) {
                visited.add(container);
                break;
            }
        }

        if (cells.length >= 10) {
            result.push({ month: monthName, year, days: cells });
        }
    }

    return result;
}
"""

async def extract_prices(page) -> list[dict]:
    try:
        data = await page.evaluate(EXTRACT_JS)
    except Exception as exc:
        print(f"Erro no evaluate JS: {exc}")
        return []

    if isinstance(data, dict) and "error" in data:
        print(f"JS retornou erro: {data['error']}")
        html = await page.content()
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        return []

    prices = []
    today = date.today()

    for month_block in data:
        raw_name = month_block.get("month", "")
        year = month_block.get("year", 0)
        days = month_block.get("days", [])

        norm = raw_name.lower().replace("ç", "c").replace("ã", "a")
        month_num = MONTHS_PT.get(norm) or MONTHS_PT.get(raw_name.lower())
        if not month_num:
            print(f"Mês não reconhecido: {raw_name!r}")
            continue

        for cell in days:
            day = cell["day"]
            try:
                d = date(year, month_num, day)
            except ValueError:
                continue
            if d < today:
                continue

            price = parse_brl(cell["price"].replace(".", "").replace(",", "."))
            if price is None:
                try:
                    price = float(cell["price"].replace(".", "").replace(",", "."))
                except ValueError:
                    continue
            if price < 100:
                continue

            prices.append({
                "year": year,
                "month": month_num,
                "day": day,
                "price": price,
                "is_weekend": is_weekend(year, month_num, day),
            })

    return prices

async def open_calendar(page) -> bool:
    """Seleciona o hotel Atibaia e abre o calendário de datas."""
    print(f"Carregando {HOTEL_URL} ...")
    await page.goto(HOTEL_URL, wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(5_000)
    await page.screenshot(path="debug_1_inicial.png")

    # Salva HTML para depuração
    html = await page.content()
    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Título da página: {await page.title()}")

    # Fecha popup de cookies/LGPD se existir
    print("Verificando popup de cookies/LGPD...")
    consent_selectors = [
        "text=Aceitar todos", "text=Aceitar", "text=Aceito",
        "text=Aceitar cookies", "text=OK", "text=Entendi",
        "[class*='accept']", "[id*='cookie'] button",
        "[id*='lgpd'] button", "[class*='cookie'] button",
        "button[class*='primary']",
    ]
    for sel in consent_selectors:
        try:
            await page.click(sel, timeout=2_000)
            print(f"Popup fechado: {sel}")
            await page.wait_for_timeout(1_000)
            break
        except PlaywrightTimeout:
            continue

    await page.screenshot(path="debug_2_apos_consent.png")

    # Tenta encontrar o seletor de hotel na página principal e em iframes
    print("Clicando em 'Escolher o hotel'...")
    selectors_hotel = [
        "text=Escolher o hotel",
        "text=Escolher hotel",
        "[class*='destination']",
        "[class*='hotel-select']",
        "[placeholder*='hotel']",
        "[placeholder*='destino']",
        "[class*='search'] [class*='select']",
    ]

    clicked = False
    # Tenta na página principal
    for sel in selectors_hotel:
        try:
            await page.click(sel, timeout=5_000)
            clicked = True
            print(f"Hotel selector encontrado: {sel}")
            break
        except PlaywrightTimeout:
            continue

    # Se não encontrou, tenta dentro de iframes
    if not clicked:
        print(f"Iframes na página: {len(page.frames)}")
        for frame in page.frames[1:]:  # pula o frame principal
            print(f"  Tentando iframe: {frame.url}")
            for sel in selectors_hotel:
                try:
                    await frame.click(sel, timeout=3_000)
                    clicked = True
                    print(f"  Encontrado em iframe: {sel}")
                    break
                except PlaywrightTimeout:
                    continue
            if clicked:
                break

    if not clicked:
        print("ERRO: não encontrou o seletor do hotel em nenhum frame")
        await page.screenshot(path="debug_erro_hotel.png")
        return False

    await page.wait_for_timeout(1_500)
    await page.screenshot(path="debug_3_dropdown.png")

    # Seleciona Atibaia
    print("Selecionando Tauá Resort Atibaia / SP...")
    selectors_atibaia = [
        "text=Tauá Resort Atibaia / SP",
        "text=Tauá Resort Atibaia",
        "text=Atibaia / SP",
        "text=Atibaia",
    ]
    clicked = False
    for frame in [page] + list(page.frames[1:]):
        for sel in selectors_atibaia:
            try:
                await frame.click(sel, timeout=5_000)
                clicked = True
                break
            except PlaywrightTimeout:
                continue
        if clicked:
            break

    if not clicked:
        print("ERRO: não encontrou a opção Atibaia")
        await page.screenshot(path="debug_erro_atibaia.png")
        return False

    await page.wait_for_timeout(1_500)
    await page.screenshot(path="debug_4_hotel_selecionado.png")

    # Abre calendário de datas
    print("Abrindo calendário de datas...")
    selectors_dates = [
        "text=Selecione as datas",
        "text=Check-in",
        "[class*='date-picker']",
        "[class*='checkin']",
        "[class*='dates']",
    ]
    clicked = False
    for frame in [page] + list(page.frames[1:]):
        for sel in selectors_dates:
            try:
                await frame.click(sel, timeout=5_000)
                clicked = True
                break
            except PlaywrightTimeout:
                continue
        if clicked:
            break

    if not clicked:
        print("AVISO: não encontrou botão de datas, talvez o calendário já esteja aberto")

    await page.wait_for_timeout(2_500)
    await page.screenshot(path="debug_5_calendario.png")
    return True



def find_promotions(prices: list[dict]) -> list[dict]:
    promos = []
    for p in prices:
        threshold = WEEKEND_MAX if p["is_weekend"] else WEEKDAY_MAX
        if p["price"] < threshold:
            promos.append(p)
    return promos

def format_message(promotions: list[dict]) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"PROMOCAO Taua Resort Atibaia - {now}"]
    for p in sorted(promotions, key=lambda x: (x["year"], x["month"], x["day"])):
        tipo = "FDS" if p["is_weekend"] else "Sem"
        limite = WEEKEND_MAX if p["is_weekend"] else WEEKDAY_MAX
        economia = limite - p["price"]
        lines.append(
            f"{p['day']:02d}/{p['month']:02d}/{p['year']} ({tipo}): "
            f"R$ {p['price']:,.0f}  (-R$ {economia:,.0f})"
        )
    return "\n".join(lines)

async def main() -> None:
    all_prices: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        ok = await open_calendar(page)
        if not ok:
            print("Não foi possível abrir o calendário. Abortando.")
            await browser.close()
            sys.exit(1)

        batch = await extract_prices(page)
        print(f"Página 1: {len(batch)} preços extraídos")
        all_prices.extend(batch)

        for i in range(EXTRA_MONTH_PAGES):
            advanced = await click_next_month(page)
            if not advanced:
                break
            await page.screenshot(path=f"debug_5_mes_extra_{i+1}.png")
            batch = await extract_prices(page)
            print(f"Página {i+2}: {len(batch)} preços extraídos")
            all_prices.extend(batch)

        await browser.close()

    print(f"\nTotal de preços coletados: {len(all_prices)}")
    promotions = find_promotions(all_prices)
    print(f"Promoções encontradas: {len(promotions)}")

    if promotions:
        msg = format_message(promotions)
        print(f"\n{msg}\n")
        await send_whatsapp(msg)
    else:
        print("Nenhuma promoção abaixo dos limites configurados.")
        if all_prices:
            sorted_prices = sorted(all_prices, key=lambda x: x["price"])
            print("\nMenores preços encontrados:")
            for p in sorted_prices[:5]:
                tipo = "FDS" if p["is_weekend"] else "Sem"
                print(f"  {p['day']:02d}/{p['month']:02d}/{p['year']} ({tipo}): R$ {p['price']:,.0f}")

if __name__ == "__main__":
    asyncio.run(main())
