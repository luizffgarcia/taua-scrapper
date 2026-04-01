#!/usr/bin/env python3
"""
Monitor de preços do Tauá Resort Atibaia / SP.
Verifica diárias e envia notificação WhatsApp via CallMeBot quando encontrar promoções.

Thresholds:
  - Dias de semana (Seg-Sex): abaixo de R$ 1.700
  - Finais de semana (Sáb-Dom): abaixo de R$ 1.700
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

PHONE = os.environ.get("PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
HOTEL_URL = "https://tauaresorts.com.br/atibaia"

WEEKDAY_MAX = 2700.0
WEEKEND_MAX = 2700.0
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
    // Strategy 1: find buttons by mantine class
    let dayButtons = document.querySelectorAll('button.mantine-DatePicker-day, button[class*="mantine-DatePicker-day"]');

    // Strategy 2: find buttons by aria-label pattern "DD monthname YYYY"
    if (dayButtons.length === 0) {
        const datePattern = /^\\d{1,2}\\s+\\S+\\s+\\d{4}$/;
        dayButtons = Array.from(document.querySelectorAll('button[aria-label]')).filter(
            btn => datePattern.test(btn.getAttribute('aria-label').trim())
        );
    }

    // Strategy 3: find buttons inside the popover that contain R$
    if (dayButtons.length === 0) {
        const popover = document.querySelector('[class*="mantine-Popover-dropdown"], [class*="mantine-DatePicker-levelsGroup"]');
        if (popover) {
            dayButtons = Array.from(popover.querySelectorAll('button')).filter(
                btn => (btn.textContent || '').includes('R$')
            );
        }
    }

    // Strategy 4: any button anywhere containing R$ with a small bounding box (calendar cell)
    if (dayButtons.length === 0) {
        dayButtons = Array.from(document.querySelectorAll('button')).filter(btn => {
            const text = btn.textContent || '';
            if (!text.includes('R$')) return false;
            const bbox = btn.getBoundingClientRect();
            return bbox.width > 30 && bbox.width < 120 && bbox.height > 30 && bbox.height < 80;
        });
    }

    if (dayButtons.length === 0) {
        // Diagnostic: dump popover contents
        const popover = document.querySelector('[class*="mantine-Popover-dropdown"]');
        const popoverInfo = popover
            ? 'Popover found, children: ' + popover.children.length + ', innerHTML length: ' + popover.innerHTML.length + ', first 500 chars: ' + popover.innerHTML.substring(0, 500)
            : 'No popover found';
        const allButtons = document.querySelectorAll('button');
        const buttonsWithR = Array.from(allButtons).filter(b => (b.textContent || '').includes('R$')).length;
        return { error: 'Nenhum botão de dia encontrado. Buttons total: ' + allButtons.length + ', with R$: ' + buttonsWithR + '. ' + popoverInfo };
    }

    const grouped = {};

    for (const btn of dayButtons) {
        // Try aria-label first: "5 abril 2026"
        const ariaLabel = (btn.getAttribute('aria-label') || '').trim();
        const match = ariaLabel.match(/^(\\d{1,2})\\s+(\\S+)\\s+(\\d{4})$/);

        let day, monthNorm, year;
        if (match) {
            day = parseInt(match[1], 10);
            const monthRaw = match[2].toLowerCase();
            year = parseInt(match[3], 10);
            monthNorm = monthRaw.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
        } else {
            // Fallback: extract day number from button text
            const spans = btn.querySelectorAll('span');
            let dayText = null;
            for (const s of spans) {
                const t = s.textContent.trim();
                if (/^\\d{1,2}$/.test(t) && parseInt(t) >= 1 && parseInt(t) <= 31) {
                    dayText = t;
                    break;
                }
            }
            if (!dayText) continue;
            day = parseInt(dayText, 10);
            // Try to get month/year from nearest header
            monthNorm = 'unknown';
            year = new Date().getFullYear();
        }

        // Find price: look for R$ in any span or text node
        let priceText = null;
        const allSpans = btn.querySelectorAll('span');
        for (const s of allSpans) {
            const t = s.textContent.trim();
            if (t.match(/R\\$\\s*[\\d.,]+/)) {
                priceText = t;
                break;
            }
        }
        if (!priceText) {
            // Try full button text
            const btnText = btn.textContent || '';
            const pm = btnText.match(/R\\$\\s*[\\d.,]+/);
            if (pm) priceText = pm[0];
        }
        if (!priceText) continue;

        const priceMatch = priceText.match(/R\\$\\s*([\\d.,]+)/);
        if (!priceMatch) continue;

        const key = monthNorm + '_' + year;
        if (!grouped[key]) {
            grouped[key] = { month: monthNorm, year: year, days: [] };
        }
        grouped[key].days.push({ day: day, price: priceMatch[1] });
    }

    const result = Object.values(grouped);
    if (result.length === 0) {
        return { error: 'Botões encontrados (' + dayButtons.length + ') mas nenhum com preço extraível' };
    }
    return result;
}
"""

async def extract_prices(page) -> list[dict]:
    # Espera os botões do calendário renderizarem
    for selector in [
        "button.mantine-DatePicker-day",
        "button[class*='mantine-DatePicker-day']",
        "button[aria-label*='abril'], button[aria-label*='maio'], button[aria-label*='março']",
    ]:
        try:
            await page.wait_for_selector(selector, timeout=5_000, state="attached")
            print(f"Botões de dia encontrados com: {selector}")
            break
        except PlaywrightTimeout:
            continue
    else:
        print("AVISO: nenhum seletor de botão de dia encontrado, tentando extração mesmo assim...")
        await page.wait_for_timeout(3_000)

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

async def dismiss_popup(page) -> None:
    """Tenta fechar qualquer popup/modal que apareça na página inicial."""
    popup_selectors = [
        "button:has-text('Fechar')",
        "button:has-text('fechar')",
        "button:has-text('Não')",
        "[aria-label='Close']",
        "[aria-label='Fechar']",
        "button.close",
        "[class*='close']",
        "[class*='dismiss']",
        "button:has-text('×')",
        "button:has-text('X')",
    ]
    for sel in popup_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1_000):
                await el.click(timeout=2_000)
                print(f"Popup fechado com: {sel}")
                await page.wait_for_timeout(1_000)
                return
        except Exception:
            continue

    # Tenta fechar clicando em overlay escuro de fundo
    try:
        closed = await page.evaluate("""
        () => {
            const overlays = document.querySelectorAll('[class*="overlay"], [class*="backdrop"], [class*="modal"]');
            for (const el of overlays) {
                const style = window.getComputedStyle(el);
                if (style.position === 'fixed' || style.position === 'absolute') {
                    el.click();
                    return true;
                }
            }
            return false;
        }
        """)
        if closed:
            print("Popup fechado via overlay click")
            await page.wait_for_timeout(1_000)
    except Exception:
        pass

    # Tenta tecla Escape como último recurso
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def open_calendar(page) -> bool:
    """Seleciona o hotel Atibaia e abre o calendário de datas."""
    print(f"Carregando {HOTEL_URL} ...")
    await page.goto(HOTEL_URL, wait_until="networkidle", timeout=60_000)
    await page.wait_for_timeout(8_000)

    # Salva HTML e screenshot para análise
    html = await page.content()
    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(html)
    await page.screenshot(path="debug_1_inicial.png", full_page=True)
    print(f"Título: {await page.title()} | URL: {page.url}")

    # Fecha popup se existir
    await dismiss_popup(page)
    await page.screenshot(path="debug_1b_pos_popup.png")

    # Dump de todos os elementos interativos visíveis
    elements = await page.evaluate("""
    () => {
        const results = [];
        const sel = 'button, input, select, a, [role="button"], [role="combobox"], [class*="search"], [class*="book"], [class*="hotel"], [class*="destino"], [class*="destination"]';
        document.querySelectorAll(sel).forEach(el => {
            const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().substring(0, 80);
            if (text) results.push({
                tag: el.tagName,
                text,
                cls: (el.className || '').substring(0, 80),
                id: el.id || ''
            });
        });
        return results.slice(0, 50);
    }
    """)
    print(f"=== {len(elements)} elementos interativos ===")
    for el in elements:
        print(f"  {el['tag']} | {el['text']!r} | class={el['cls']!r} | id={el['id']!r}")
    print("===")

    # --- Passo 1: Clicar no botão DESTINO para abrir o dropdown ---
    print("Clicando em 'Escolher o hotel'...")
    selectors_hotel = [
        "button:has-text('Escolher o hotel')",
        "text=Escolher o hotel",
        "button:has-text('DESTINO')",
        "text=DESTINO",
    ]

    clicked = False
    for sel in selectors_hotel:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                await el.click(timeout=5_000)
                clicked = True
                print(f"Clicado: {sel}")
                break
        except (PlaywrightTimeout, Exception):
            continue

    if not clicked:
        # Fallback: tenta click via JavaScript no botão que contém "Escolher o hotel"
        try:
            clicked = await page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.innerText && btn.innerText.includes('Escolher o hotel')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
            """)
            if clicked:
                print("Clicado via JS: botão 'Escolher o hotel'")
        except Exception:
            pass

    if not clicked:
        print("ERRO: não encontrou o seletor do hotel")
        await page.screenshot(path="debug_erro_hotel.png")
        return False

    await page.wait_for_timeout(2_000)
    await page.screenshot(path="debug_2_dropdown.png")

    # --- Passo 2: Selecionar Tauá Resort Atibaia / SP no dropdown ---
    print("Selecionando Tauá Resort Atibaia / SP...")

    # O dropdown contém <p> elements com os nomes dos hotéis
    # IMPORTANTE: usar Playwright click (não JS click) para disparar eventos React
    selectors_atibaia = [
        "p:has-text('Tauá Resort Atibaia / SP')",
        "p:has-text('Tauá Resort Atibaia')",
        "text=Tauá Resort Atibaia / SP",
        "text=Tauá Resort Atibaia",
    ]
    clicked = False
    for sel in selectors_atibaia:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3_000):
                await el.click(timeout=4_000)
                clicked = True
                print(f"Selecionado: {sel}")
                break
        except (PlaywrightTimeout, Exception) as e:
            print(f"  Falhou {sel}: {e}")
            continue

    if not clicked:
        # Fallback: usar dispatchEvent para simular click com eventos React
        try:
            clicked = await page.evaluate("""
            () => {
                const paragraphs = document.querySelectorAll('p');
                for (const p of paragraphs) {
                    const text = (p.textContent || '').trim();
                    if (text.includes('Atibaia') && text.includes('SP')) {
                        // Dispatch mousedown/mouseup/click para React
                        ['mousedown', 'mouseup', 'click'].forEach(evtName => {
                            p.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true }));
                        });
                        return true;
                    }
                }
                return false;
            }
            """)
            if clicked:
                print("Selecionado via dispatchEvent: Atibaia / SP")
        except Exception:
            pass

    if not clicked:
        print("ERRO: não encontrou a opção Atibaia")
        await page.screenshot(path="debug_erro_atibaia.png")
        return False

    await page.wait_for_timeout(3_000)
    await page.screenshot(path="debug_3_hotel_selecionado.png")

    # --- Passo 3: Esperar o grid do calendário carregar ---
    # Após selecionar o hotel, o calendário com preços deve aparecer automaticamente
    # O grid tem classe "mantine-DatePicker-levelsGroup"
    print("Esperando calendário carregar...")

    calendar_loaded = False
    # Primeiro tenta esperar o grid aparecer diretamente
    try:
        await page.wait_for_selector(
            '[class*="mantine-DatePicker-levelsGroup"]',
            timeout=10_000,
            state="visible"
        )
        calendar_loaded = True
        print("Grid do calendário encontrado!")
    except PlaywrightTimeout:
        print("Grid não apareceu diretamente, tentando clicar em DATAS...")

    if not calendar_loaded:
        # Tenta clicar no botão DATAS para abrir o calendário
        selectors_dates = [
            "button:has-text('Selecione as datas')",
            "text=Selecione as datas",
            "button:has-text('DATAS')",
        ]
        for sel in selectors_dates:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=3_000):
                    await el.click(timeout=4_000)
                    print(f"Clicado: {sel}")
                    break
            except (PlaywrightTimeout, Exception):
                continue

        # Espera novamente pelo grid
        try:
            await page.wait_for_selector(
                '[class*="mantine-DatePicker-levelsGroup"]',
                timeout=10_000,
                state="visible"
            )
            calendar_loaded = True
            print("Grid do calendário encontrado após clicar DATAS!")
        except PlaywrightTimeout:
            pass

    if not calendar_loaded:
        # Verifica se há botões com R$ na página (calendário pode ter outro seletor)
        has_prices = await page.evaluate("""
        () => {
            const buttons = document.querySelectorAll('button');
            let count = 0;
            for (const b of buttons) {
                if ((b.textContent || '').includes('R$')) count++;
            }
            return count;
        }
        """)
        print(f"Botões com R$ na página: {has_prices}")
        if has_prices > 0:
            calendar_loaded = True

    await page.screenshot(path="debug_4_calendario.png")

    if not calendar_loaded:
        print("ERRO: calendário não carregou")
        return False

    print("Calendário carregado com sucesso!")
    return True


async def click_next_month(page) -> bool:
    """Clica no botão de próximo mês no calendário Mantine DatePicker."""
    try:
        # Mantine DatePicker usa botões com aria-label ou data-next para navegar
        next_selectors = [
            "[class*='mantine-DatePicker-calendarHeaderControl'][data-direction='next']",
            "[class*='calendarHeaderControl'][data-next]",
            "button[data-direction='next']",
            "[class*='mantine-DatePicker'] button[aria-label='Next month']",
            "[class*='mantine-DatePicker'] button:has-text('>')",
        ]
        for sel in next_selectors:
            try:
                el = page.locator(sel).last  # last = rightmost month's next button
                if await el.is_visible(timeout=2_000):
                    await el.click(timeout=3_000)
                    print(f"Próximo mês clicado com: {sel}")
                    await page.wait_for_timeout(2_000)
                    return True
            except (PlaywrightTimeout, Exception):
                continue

        # Fallback via JS: procura botões de controle do calendário Mantine
        clicked = await page.evaluate("""
        () => {
            // Mantine calendar header controls
            const controls = document.querySelectorAll('[class*="calendarHeaderControl"]');
            // O último botão de "next" geralmente é o da direita
            for (const ctrl of controls) {
                const dir = ctrl.getAttribute('data-direction');
                if (dir === 'next') {
                    ctrl.click();
                    return true;
                }
            }
            // Fallback: tenta o último botão com seta para direita
            const arrows = document.querySelectorAll('button svg');
            if (arrows.length > 0) {
                const lastArrow = arrows[arrows.length - 1];
                lastArrow.closest('button').click();
                return true;
            }
            return false;
        }
        """)
        if clicked:
            print("Próximo mês clicado via JS")
            await page.wait_for_timeout(2_000)
            return True

        print("AVISO: não conseguiu avançar para o próximo mês")
        return False
    except Exception as exc:
        print(f"Erro ao clicar próximo mês: {exc}")
        return False

def find_promotions(prices: list[dict]) -> list[dict]:
    promos = []
    for p in prices:
        threshold = WEEKEND_MAX if p["is_weekend"] else WEEKDAY_MAX
        if p["price"] < threshold:
            promos.append(p)
    return promos

def format_message(promotions: list[dict]) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [f"PROMOCAO Taua Atibaia - {now}"]
    sorted_promos = sorted(promotions, key=lambda x: x["price"])[:10]
    for p in sorted_promos:
        tipo = "FDS" if p["is_weekend"] else "Sem"
        lines.append(
            f"{p['day']:02d}/{p['month']:02d}/{p['year']} ({tipo}): "
            f"R${p['price']:,.0f}"
        )
    if len(promotions) > 10:
        lines.append(f"...e mais {len(promotions) - 10} datas")
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
