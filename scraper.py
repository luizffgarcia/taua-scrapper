#!/usr/bin/env python3
"""
Monitor de preços do Tauá Resort Atibaia / SP.
Verifica diárias e envia notificação WhatsApp via CallMeBot quando encontrar promoções.

Thresholds:
  - Dias de semana (Seg-Sex): abaixo de R$ 1.700
  - Finais de semana (Sáb-Dom): abaixo de R$ 1.900
"""

import asyncio
import re
import os
import sys
import json
import base64
import urllib.parse
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

PHONE = os.environ.get("PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "luizffgarcia/taua-scrapper"
STATE_PATH = "last_notification.json"
HOTEL_URL = "https://tauaresorts.com.br/atibaia"

WEEKDAY_MAX = 1700.0
WEEKEND_MAX = 1900.0
EXTRA_MONTH_PAGES = 1
NOTIFICATION_COOLDOWN_HOURS = 24

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


# ── Estado de notificação (persiste no repositório via GitHub API) ─────────────

async def get_last_notification() -> tuple[Optional[datetime], Optional[str]]:
    """Lê o timestamp da última notificação do arquivo de estado no repo.
    Retorna (datetime_utc, sha_do_arquivo) ou (None, None) se não existir."""
    if not GITHUB_TOKEN:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                content = base64.b64decode(data["content"]).decode()
                state = json.loads(content)
                last_sent = datetime.fromisoformat(state["last_sent"])
                return last_sent, data["sha"]
            return None, None
        except Exception as exc:
            print(f"Aviso ao ler estado: {exc}")
            return None, None


async def save_last_notification(sha: Optional[str]) -> None:
    """Salva o timestamp atual como última notificação enviada."""
    if not GITHUB_TOKEN:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    content_bytes = json.dumps({"last_sent": now_iso}).encode()
    encoded = base64.b64encode(content_bytes).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    body: dict = {"message": "chore: update last notification timestamp", "content": encoded}
    if sha:
        body["sha"] = sha
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.put(url, headers=headers, json=body)
            if resp.status_code in (200, 201):
                print(f"Estado de notificação salvo ({now_iso})")
            else:
                print(f"Aviso ao salvar estado: HTTP {resp.status_code} - {resp.text[:200]}")
        except Exception as exc:
            print(f"Erro ao salvar estado: {exc}")


# ── WhatsApp ──────────────────────────────────────────────────────────────────

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


# ── Extração de preços do calendário ─────────────────────────────────────────

EXTRACT_JS = """
() => {
    let dayButtons = document.querySelectorAll('button.mantine-DatePicker-day, button[class*="mantine-DatePicker-day"]');

    if (dayButtons.length === 0) {
        const datePattern = /^\\d{1,2}\\s+\\S+\\s+\\d{4}$/;
        dayButtons = Array.from(document.querySelectorAll('button[aria-label]')).filter(
            btn => datePattern.test(btn.getAttribute('aria-label').trim())
        );
    }

    if (dayButtons.length === 0) {
        const popover = document.querySelector('[class*="mantine-Popover-dropdown"], [class*="mantine-DatePicker-levelsGroup"]');
        if (popover) {
            dayButtons = Array.from(popover.querySelectorAll('button')).filter(
                btn => (btn.textContent || '').includes('R$')
            );
        }
    }

    if (dayButtons.length === 0) {
        dayButtons = Array.from(document.querySelectorAll('button')).filter(btn => {
            const text = btn.textContent || '';
            if (!text.includes('R$')) return false;
            const bbox = btn.getBoundingClientRect();
            return bbox.width > 30 && bbox.width < 120 && bbox.height > 30 && bbox.height < 80;
        });
    }

    if (dayButtons.length === 0) {
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
        const ariaLabel = (btn.getAttribute('aria-label') || '').trim();
        const match = ariaLabel.match(/^(\\d{1,2})\\s+(\\S+)\\s+(\\d{4})$/);

        let day, monthNorm, year;
        if (match) {
            day = parseInt(match[1], 10);
            const monthRaw = match[2].toLowerCase();
            year = parseInt(match[3], 10);
            monthNorm = monthRaw.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
        } else {
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
            monthNorm = 'unknown';
            year = new Date().getFullYear();
        }

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


# ── Navegação ─────────────────────────────────────────────────────────────────

async def dismiss_popup(page) -> None:
    popup_selectors = [
        "button:has-text('Fechar')", "button:has-text('fechar')",
        "button:has-text('Não')", "[aria-label='Close']", "[aria-label='Fechar']",
        "button.close", "[class*='close']", "[class*='dismiss']",
        "button:has-text('×')", "button:has-text('X')",
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
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def open_calendar(page) -> bool:
    print(f"Carregando {HOTEL_URL} ...")
    await page.goto(HOTEL_URL, wait_until="networkidle", timeout=60_000)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(3_000)
    await page.screenshot(path="debug_1_inicial.png")
    print(f"Título: {await page.title()}")

    await dismiss_popup(page)

    print("Aguardando widget de reservas carregar...")
    widget_found = False
    for sel in ["text=Escolher o hotel", "button:has-text('Escolher o hotel')"]:
        try:
            await page.wait_for_selector(sel, timeout=30_000, state="visible")
            widget_found = True
            print(f"Widget encontrado: {sel}")
            break
        except PlaywrightTimeout:
            continue

    if not widget_found:
        print("Widget não apareceu, tentando via 'RESERVE AGORA'...")
        try:
            await page.click("button:has-text('RESERVE AGORA')", timeout=5_000)
            await page.wait_for_timeout(2_000)
            await page.wait_for_selector("text=Escolher o hotel", timeout=15_000, state="visible")
            widget_found = True
            print("Widget aberto via RESERVE AGORA!")
        except PlaywrightTimeout:
            print("ERRO: widget de reservas não carregou após 30s")
            await page.screenshot(path="debug_erro_widget.png")
            return False

    await page.wait_for_timeout(2_000)

    print("Clicando em 'Escolher o hotel'...")
    clicked = False
    for sel in ["text=Escolher o hotel", "button:has-text('Escolher o hotel')", "[class*='destination']"]:
        try:
            await page.locator(sel).first.click(timeout=5_000)
            clicked = True
            print(f"Clicado: {sel}")
            break
        except Exception:
            continue

    if not clicked:
        try:
            clicked = await page.evaluate("""
            () => {
                for (const btn of document.querySelectorAll('button')) {
                    if (btn.innerText && btn.innerText.includes('Escolher o hotel')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
            """)
            if clicked:
                print("Clicado via JS: 'Escolher o hotel'")
        except Exception:
            pass

    if not clicked:
        print("ERRO: não encontrou o seletor do hotel")
        await page.screenshot(path="debug_erro_hotel.png")
        return False

    await page.wait_for_timeout(2_000)
    await page.screenshot(path="debug_2_dropdown.png")

    print("Selecionando Tauá Resort Atibaia / SP...")
    clicked = False
    for sel in [
        "p:has-text('Tauá Resort Atibaia / SP')", "p:has-text('Tauá Resort Atibaia')",
        "text=Tauá Resort Atibaia / SP", "text=Tauá Resort Atibaia",
    ]:
        try:
            await page.locator(sel).first.click(timeout=5_000)
            clicked = True
            print(f"Selecionado: {sel}")
            break
        except Exception:
            continue

    if not clicked:
        try:
            clicked = await page.evaluate("""
            () => {
                for (const p of document.querySelectorAll('p')) {
                    const text = (p.textContent || '').trim();
                    if (text.includes('Atibaia') && text.includes('SP')) {
                        ['mousedown', 'mouseup', 'click'].forEach(evt =>
                            p.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true }))
                        );
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

    await page.wait_for_timeout(2_000)
    await page.screenshot(path="debug_3_hotel_selecionado.png")

    print("Esperando calendário carregar...")
    calendar_loaded = False

    try:
        await page.wait_for_selector(
            '[class*="mantine-DatePicker-levelsGroup"]',
            timeout=15_000, state="visible"
        )
        calendar_loaded = True
        print("Grid do calendário encontrado!")
    except PlaywrightTimeout:
        print("Grid não apareceu, tentando clicar em DATAS...")
        for sel in ["text=Selecione as datas", "button:has-text('Selecione as datas')"]:
            try:
                await page.locator(sel).first.click(timeout=5_000)
                await page.wait_for_timeout(2_000)
                break
            except Exception:
                continue
        try:
            await page.wait_for_selector(
                '[class*="mantine-DatePicker-levelsGroup"]',
                timeout=15_000, state="visible"
            )
            calendar_loaded = True
            print("Grid do calendário encontrado após clicar DATAS!")
        except PlaywrightTimeout:
            pass

    if not calendar_loaded:
        has_prices = await page.evaluate("""
        () => Array.from(document.querySelectorAll('button')).filter(b => (b.textContent||'').includes('R$')).length
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
    try:
        next_selectors = [
            "[class*='mantine-DatePicker-calendarHeaderControl'][data-direction='next']",
            "[class*='calendarHeaderControl'][data-next]",
            "button[data-direction='next']",
            "[class*='mantine-DatePicker'] button[aria-label='Next month']",
            "[class*='mantine-DatePicker'] button:has-text('>')",
        ]
        for sel in next_selectors:
            try:
                el = page.locator(sel).last
                if await el.is_visible(timeout=2_000):
                    await el.click(timeout=3_000)
                    print(f"Próximo mês clicado com: {sel}")
                    await page.wait_for_timeout(2_000)
                    return True
            except (PlaywrightTimeout, Exception):
                continue

        clicked = await page.evaluate("""
        () => {
            const controls = document.querySelectorAll('[class*="calendarHeaderControl"]');
            for (const ctrl of controls) {
                if (ctrl.getAttribute('data-direction') === 'next') {
                    ctrl.click();
                    return true;
                }
            }
            const arrows = document.querySelectorAll('button svg');
            if (arrows.length > 0) {
                arrows[arrows.length - 1].closest('button').click();
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


# ── Lógica principal ──────────────────────────────────────────────────────────

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
            f"R$ {p['price']:.0f}"
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

    if not promotions:
        print("Nenhuma promoção abaixo dos limites configurados.")
        if all_prices:
            sorted_prices = sorted(all_prices, key=lambda x: x["price"])
            print("\nMenores preços encontrados:")
            for p in sorted_prices[:5]:
                tipo = "FDS" if p["is_weekend"] else "Sem"
                print(f"  {p['day']:02d}/{p['month']:02d}/{p['year']} ({tipo}): R$ {p['price']:,.0f}")
        return

    # Verifica cooldown de 24 horas antes de enviar
    last_sent, state_sha = await get_last_notification()
    if last_sent:
        now_utc = datetime.now(timezone.utc)
        # garante que last_sent tem timezone
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        hours_since = (now_utc - last_sent).total_seconds() / 3600
        if hours_since < NOTIFICATION_COOLDOWN_HOURS:
            remaining = NOTIFICATION_COOLDOWN_HOURS - hours_since
            print(f"Promoções encontradas, mas notificação enviada há {hours_since:.1f}h. "
                  f"Próximo envio em {remaining:.1f}h.")
            return

    msg = format_message(promotions)
    print(f"\n{msg}\n")
    await send_whatsapp(msg)
    await save_last_notification(state_sha)


if __name__ == "__main__":
    asyncio.run(main())
