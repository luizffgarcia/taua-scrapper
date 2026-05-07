#!/usr/bin/env python3
"""
Monitor de preço — Vitamina C Lipossomal Dr. Mercola (iHerb BR).
Envia notificação WhatsApp via CallMeBot quando o preço cair abaixo de R$ 150.
"""

import asyncio
import re
import os
import sys
import json
import base64
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

PHONE = os.environ.get("PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "luizffgarcia/taua-scrapper"

PRODUCT_URL = (
    "https://br.iherb.com/pr/dr-mercola-liposomal-vitamin-c-citrus-vanilla-"
    "15-2-fl-oz-450-ml/113102"
)
PRODUCT_NAME = "Vit C Lipossomal Dr. Mercola"
PRICE_THRESHOLD = 150.0
STATE_PATH = "iherb_last_notification.json"
NOTIFICATION_COOLDOWN_HOURS = 24


def parse_brl(text: str) -> Optional[float]:
    """Extrai um valor em R$ no formato brasileiro (1.234,56)."""
    if not text:
        return None
    m = re.search(r"R\$\s*([\d.]+,\d{2})", text)
    if m:
        try:
            return float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            return None
    m = re.search(r"([\d.]+,\d{2})", text)
    if m:
        try:
            return float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            return None
    return None


def parse_decimal(text: str) -> Optional[float]:
    """Parse de número decimal (ex.: '149.50' do JSON-LD)."""
    if not text:
        return None
    try:
        return float(str(text).replace(",", "."))
    except (ValueError, TypeError):
        return None


# ── Estado de notificação (persiste no repositório via GitHub API) ───────────

async def get_last_notification() -> tuple[Optional[datetime], Optional[str]]:
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


async def save_last_notification(sha: Optional[str], price: float) -> None:
    if not GITHUB_TOKEN:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {"last_sent": now_iso, "last_price": price}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    body: dict = {
        "message": "chore: update iherb last notification timestamp",
        "content": encoded,
    }
    if sha:
        body["sha"] = sha
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.put(url, headers=headers, json=body)
            if resp.status_code in (200, 201):
                print(f"Estado de notificação salvo ({now_iso}, R$ {price:.2f})")
            else:
                print(f"Aviso ao salvar estado: HTTP {resp.status_code} - {resp.text[:200]}")
        except Exception as exc:
            print(f"Erro ao salvar estado: {exc}")


# ── WhatsApp ─────────────────────────────────────────────────────────────────

async def send_whatsapp(msg: str) -> None:
    if not CALLMEBOT_APIKEY or not PHONE:
        print(f"[SEM APIKEY/PHONE] Mensagem que seria enviada:\n{msg}")
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
            if resp.status_code != 200:
                print(f"Resposta: {resp.text[:300]}")
        except Exception as exc:
            print(f"Erro ao enviar WhatsApp: {exc}")


# ── Extração do preço (múltiplas estratégias) ────────────────────────────────

EXTRACT_JS = r"""
() => {
    const result = { jsonld: null, meta: null, visible: null, currency: null, debug: [] };

    // 1) JSON-LD (Schema.org Product/Offer)
    try {
        const scripts = document.querySelectorAll('script[type="application/ld+json"]');
        for (const s of scripts) {
            try {
                const raw = (s.textContent || '').trim();
                if (!raw) continue;
                const data = JSON.parse(raw);
                const items = Array.isArray(data) ? data : [data];
                for (const item of items) {
                    const t = item['@type'];
                    const isProduct = (Array.isArray(t) ? t.includes('Product') : t === 'Product');
                    if (!isProduct) continue;
                    let offers = item.offers;
                    if (!offers) continue;
                    const offerArr = Array.isArray(offers) ? offers : [offers];
                    for (const o of offerArr) {
                        if (o && (o.price || o.lowPrice)) {
                            result.jsonld = String(o.price || o.lowPrice);
                            result.currency = o.priceCurrency || null;
                            break;
                        }
                    }
                    if (result.jsonld) break;
                }
                if (result.jsonld) break;
            } catch (e) {
                result.debug.push('jsonld parse fail: ' + e.message);
            }
        }
    } catch (e) {
        result.debug.push('jsonld outer fail: ' + e.message);
    }

    // 2) <meta itemprop="price">
    try {
        const meta = document.querySelector('meta[itemprop="price"]');
        if (meta) result.meta = meta.getAttribute('content');
        const metaCur = document.querySelector('meta[itemprop="priceCurrency"]');
        if (metaCur && !result.currency) result.currency = metaCur.getAttribute('content');
    } catch (e) {
        result.debug.push('meta fail: ' + e.message);
    }

    // 3) Elementos visíveis com R$
    try {
        const candidates = [
            '#price',
            '.product-summary-price',
            '[id*="price"]',
            '.b2c-price',
            '.discount-price',
            '[class*="price"]'
        ];
        const seen = new Set();
        for (const sel of candidates) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (!txt || seen.has(txt)) continue;
                seen.add(txt);
                if (txt.includes('R$')) {
                    result.visible = txt;
                    break;
                }
            }
            if (result.visible) break;
        }

        // Fallback: regex no body inteiro
        if (!result.visible) {
            const bodyText = document.body.innerText || '';
            const m = bodyText.match(/R\$\s*[\d.]+,\d{2}/);
            if (m) result.visible = m[0];
        }
    } catch (e) {
        result.debug.push('visible fail: ' + e.message);
    }

    return result;
}
"""


async def fetch_price() -> Optional[float]:
    """Abre a página do produto e extrai o preço atual em BRL."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        )
        # Mascarar webdriver
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        page = await ctx.new_page()
        try:
            print(f"Carregando {PRODUCT_URL} ...")
            await page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeout:
                pass

            await page.wait_for_timeout(2_000)

            # Salva screenshot para debug
            try:
                await page.screenshot(path="iherb_debug.png", full_page=False)
            except Exception:
                pass

            print(f"Título: {await page.title()}")

            data = await page.evaluate(EXTRACT_JS)
        except PlaywrightTimeout as exc:
            print(f"Timeout na navegação: {exc}")
            await browser.close()
            return None
        finally:
            await browser.close()

    if data.get("debug"):
        for line in data["debug"]:
            print(f"  debug JS: {line}")

    print(f"  jsonld:  {data.get('jsonld')!r} ({data.get('currency')})")
    print(f"  meta:    {data.get('meta')!r}")
    print(f"  visible: {data.get('visible')!r}")

    currency = (data.get("currency") or "").upper()

    # Prioridade: JSON-LD (mais confiável) — mas só se a moeda for BRL
    if data.get("jsonld") and (currency in ("", "BRL")):
        v = parse_decimal(data["jsonld"])
        if v is not None:
            return v

    # itemprop=price (mesma checagem)
    if data.get("meta") and (currency in ("", "BRL")):
        v = parse_decimal(data["meta"])
        if v is not None:
            return v

    # Texto visível com R$ — sempre BRL
    if data.get("visible"):
        v = parse_brl(data["visible"])
        if v is not None:
            return v

    # Última tentativa: jsonld/meta mesmo se moeda não veio explícita
    for key in ("jsonld", "meta"):
        v = parse_decimal(data.get(key))
        if v is not None:
            return v

    return None


# ── Lógica principal ─────────────────────────────────────────────────────────

def format_message(price: float) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    return (
        f"PROMOCAO iHerb - {now}\n"
        f"{PRODUCT_NAME}\n"
        f"Preco: R$ {price:.2f} (abaixo de R$ {PRICE_THRESHOLD:.0f})\n"
        f"{PRODUCT_URL}"
    )


async def main() -> None:
    price = await fetch_price()
    if price is None:
        print("ERRO: não foi possível extrair o preço.")
        sys.exit(1)

    print(f"\nPreço atual: R$ {price:.2f}  (limite: R$ {PRICE_THRESHOLD:.2f})")

    if price >= PRICE_THRESHOLD:
        print("Preço acima ou igual ao limite. Sem notificação.")
        return

    # Cooldown de 24h para não floodar
    last_sent, state_sha = await get_last_notification()
    if last_sent:
        now_utc = datetime.now(timezone.utc)
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        hours_since = (now_utc - last_sent).total_seconds() / 3600
        if hours_since < NOTIFICATION_COOLDOWN_HOURS:
            remaining = NOTIFICATION_COOLDOWN_HOURS - hours_since
            print(
                f"Promoção encontrada, mas notificação enviada há {hours_since:.1f}h. "
                f"Próximo envio em {remaining:.1f}h."
            )
            return

    msg = format_message(price)
    print(f"\n{msg}\n")
    await send_whatsapp(msg)
    await save_last_notification(state_sha, price)


if __name__ == "__main__":
    asyncio.run(main())
