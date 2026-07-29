"""
Mihai's Farm — organic honey shop with real checkout
======================================================
Product/pricing/copy pulled directly from the live Etsy shop (MihaiFarm) so the
two stay consistent. Cart is stored in the Flask session; payment is handled by
Stripe Checkout (redirect flow — card details never touch this server).

Environment variables to set in Render:
  STRIPE_SECRET_KEY     - Stripe secret key (starts sk_...). Without this,
                           checkout shows a friendly "not switched on yet" message
                           instead of erroring, so the rest of the site still works.
  STRIPE_WEBHOOK_SECRET - Stripe webhook signing secret (starts whsec_...), from
                           the webhook endpoint you create in the Stripe dashboard
                           pointing at /webhook/stripe
  RESEND_API_KEY        - Resend key, sends the order notification + customer
                           confirmation emails
  NOTIFY_TO              - where new-order emails go (defaults to
                           andradudan4@gmail.com for testing — change this to
                           Mihai's real inbox before going live)
  RESEND_FROM            - the "from" address (defaults to the frontdesk.org.uk
                           sender already verified in Resend)
  SECRET_KEY             - Flask session secret (any random string)

Stripe test cards (when STRIPE_SECRET_KEY is a test key, sk_test_...):
  4242 4242 4242 4242, any future expiry, any CVC, any postcode.
"""

from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for, Response
import os
import html
import uuid
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-later")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

try:
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
except ImportError:
    stripe = None

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
NOTIFY_TO = os.environ.get("NOTIFY_TO", "andradudan4@gmail.com")
RESEND_FROM = os.environ.get("RESEND_FROM", "Mihai's Farm <leads@frontdesk.org.uk>")

SHOP_NAME = "Mihai's Farm"

# --- Product catalogue --------------------------------------------------------
# Structured so a second/third product is just another entry in PRODUCTS.
PRODUCTS = [
    {
        "slug": "acacia-rapeseed-honey",
        "name": "Organic Acacia & Rapeseed Honey",
        "tagline": "Raw, unfiltered, EU-certified organic honey from the Carpathian fields.",
        "images": [
            "product/jar-field.jpg",
            "product/honeycomb-macro.avif",
            "product/bees-on-comb.jpg",
        ],
        "bullets": [
            ("🌱", "EU Certified Organic"),
            ("🍯", "Raw and unprocessed"),
            ("🚫", "No additives or preservatives"),
            ("📱", "Certification verification via QR code"),
            ("🐝", "Produced with respect for bees and nature"),
        ],
        "description": (
            "Our EU Certified Organic Acacia and Rapeseed Honey is raw, unfiltered "
            "and carefully selected to preserve its natural flavour, aroma and "
            "character. Each jar includes a QR code allowing you to verify our "
            "organic certification directly, for complete transparency and trust."
        ),
        "varieties": [
            {
                "key": "acacia",
                "label": "Acacia Honey",
                "emoji": "🌼",
                "blurb": (
                    "Light, delicate and naturally floral, Acacia Honey is known "
                    "for its smooth texture and gentle sweetness. Naturally high "
                    "in fructose, it remains liquid for longer than most honey "
                    "varieties."
                ),
            },
            {
                "key": "rapeseed",
                "label": "Rapeseed Honey",
                "emoji": "🌾",
                "blurb": (
                    "Naturally creamy and velvety, Rapeseed Honey offers a mild "
                    "flavour and smooth texture. It crystallises into a rich, "
                    "spreadable consistency loved by many honey enthusiasts."
                ),
            },
        ],
        "storage": (
            "Store in a cool, dry place away from direct sunlight. Natural "
            "crystallisation is a sign of genuine, minimally processed honey and "
            "may occur over time."
        ),
        "variants": [
            {"id": "acacia-350", "variety": "acacia", "size": "350g", "price": 999, "label": "Acacia Honey — 350g"},
            {"id": "acacia-900", "variety": "acacia", "size": "900g", "price": 2099, "label": "Acacia Honey — 900g"},
            {"id": "rapeseed-350", "variety": "rapeseed", "size": "350g", "price": 799, "label": "Rapeseed Honey — 350g"},
            {"id": "rapeseed-900", "variety": "rapeseed", "size": "900g", "price": 1799, "label": "Rapeseed Honey — 900g"},
        ],
    },
]

SHIPPING_PENCE = 300  # flat £3 UK postage, matches the Etsy listing


def gbp(pence):
    return f"£{pence / 100:,.2f}"


def find_product(slug):
    return next((p for p in PRODUCTS if p["slug"] == slug), None)


def find_variant(variant_id):
    for product in PRODUCTS:
        for variant in product["variants"]:
            if variant["id"] == variant_id:
                return product, variant
    return None, None


def get_cart():
    return session.get("cart", {})


def cart_lines():
    lines = []
    for variant_id, qty in get_cart().items():
        product, variant = find_variant(variant_id)
        if not product:
            continue
        lines.append({
            "product": product,
            "variant": variant,
            "qty": qty,
            "line_total": variant["price"] * qty,
        })
    return lines


def cart_count():
    return sum(get_cart().values())


def cart_subtotal():
    return sum(l["line_total"] for l in cart_lines())


# --- Email (Resend) -----------------------------------------------------------

def _post_resend(to, subject, text, html_body=None):
    if not RESEND_API_KEY:
        print(f"RESEND_API_KEY not set, skipping email to {to}: {subject}")
        return
    payload = {"from": RESEND_FROM, "to": [to], "subject": subject, "text": text}
    if html_body:
        payload["html"] = html_body
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json=payload, timeout=15,
        )
        if r.status_code >= 300:
            print(f"Resend error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def _order_rows_html(line_items):
    rows = []
    for li in line_items:
        rows.append(
            '<tr>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #eee;font-size:14px">{html.escape(li["name"])}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #eee;font-size:14px;text-align:center">{li["qty"]}</td>'
            f'<td style="padding:9px 14px;border-bottom:1px solid #eee;font-size:14px;text-align:right">{gbp(li["amount"])}</td>'
            '</tr>'
        )
    return "".join(rows)


def _order_email_shell(eyebrow, title, inner):
    return (
        '<!DOCTYPE html><html><body style="margin:0;background:#f3ede0;padding:24px;'
        'font-family:Georgia,\'Times New Roman\',serif">'
        '<div style="max-width:600px;margin:0 auto;background:#fffdf8;border-radius:10px;'
        'overflow:hidden;border:1px solid #e6dcc4">'
        '<div style="background:#1f3a24;padding:22px 28px">'
        f'<div style="color:#d4af37;font-size:12px;letter-spacing:.2em;text-transform:uppercase">{eyebrow}</div>'
        f'<div style="color:#fdfaf2;font-size:20px;margin-top:6px">{title}</div></div>'
        f'<div style="padding:24px 28px">{inner}</div>'
        '</div></body></html>'
    )


def send_order_emails(checkout_session):
    """Called from the Stripe webhook once a payment has actually completed."""
    session_id = checkout_session["id"]
    customer_email = (checkout_session.get("customer_details") or {}).get("email") or "Not provided"
    customer_name = (checkout_session.get("customer_details") or {}).get("name") or "Not provided"
    shipping = checkout_session.get("shipping_details") or {}
    address = (shipping.get("address") or {})
    address_lines = ", ".join(filter(None, [
        address.get("line1"), address.get("line2"), address.get("city"),
        address.get("postal_code"), address.get("country"),
    ]))
    total = checkout_session.get("amount_total", 0)

    line_items = []
    if stripe:
        try:
            items = stripe.checkout.Session.list_line_items(session_id, limit=100)
            for it in items.get("data", []):
                line_items.append({"name": it["description"], "qty": it["quantity"], "amount": it["amount_total"]})
        except Exception as e:
            print(f"Could not fetch line items: {e}")

    rows = _order_rows_html(line_items)
    owner_inner = (
        f'<p style="margin:0 0 16px;font-size:14px;color:#555">New order — <strong>{gbp(total)}</strong> total.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:18px">{rows}</table>'
        f'<p style="margin:4px 0;font-size:14px"><strong>Customer:</strong> {html.escape(customer_name)} ({html.escape(customer_email)})</p>'
        f'<p style="margin:4px 0;font-size:14px"><strong>Ship to:</strong> {html.escape(address_lines) or "Not provided"}</p>'
        f'<p style="margin:14px 0 0;font-size:12px;color:#999">Stripe session: {session_id}</p>'
    )
    _post_resend(
        NOTIFY_TO,
        f"New order — {gbp(total)} — {customer_name}",
        f"New order from {customer_name} ({customer_email}) — {gbp(total)}\nShip to: {address_lines}",
        _order_email_shell("Mihai's Farm", "New order received", owner_inner),
    )

    if customer_email and customer_email != "Not provided":
        cust_inner = (
            f'<p style="margin:0 0 16px;font-size:14px;color:#555">Thank you {html.escape(customer_name.split(" ")[0] if customer_name != "Not provided" else "")} — '
            'your order is confirmed and will be packed with care.</p>'
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:18px">{rows}</table>'
            f'<p style="margin:4px 0;font-size:14px"><strong>Total paid:</strong> {gbp(total)}</p>'
            f'<p style="margin:4px 0;font-size:14px"><strong>Delivery address:</strong> {html.escape(address_lines) or "Not provided"}</p>'
        )
        _post_resend(
            customer_email,
            "Your Mihai's Farm order is confirmed",
            f"Thank you for your order — {gbp(total)} total. We'll be in touch if anything needs confirming.",
            _order_email_shell("Mihai's Farm", "Order confirmed — thank you", cust_inner),
        )


def send_contact_email(name, email, message):
    inner = (
        f'<p style="margin:4px 0;font-size:14px"><strong>From:</strong> {html.escape(name)} ({html.escape(email)})</p>'
        f'<p style="margin:14px 0 0;font-size:14px;white-space:pre-wrap">{html.escape(message)}</p>'
    )
    _post_resend(NOTIFY_TO, f"Website message from {name}", message, _order_email_shell(SHOP_NAME, "New contact message", inner))


# --- Look & feel ----------------------------------------------------------------

BASE_STYLE = """
<link rel="icon" type="image/jpeg" href="/static/images/logo.jpg">
<meta name="theme-color" content="#1f3a24">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Mihai's Farm">
<meta property="og:image" content="/static/images/banner.jpg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700;800&family=Jost:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --cream:#faf5e9; --paper:#fffdf8; --ink:#2a2118; --mut:#6b6152;
    --green:#1f3a24; --green-dk:#0e1f13; --gold:#e3ac0d; --gold-lt:#ffe37a; --gold-dk:#a97e05;
    --gold-glow:rgba(227,172,13,.55);
    --line:rgba(31,58,36,.14);
  }
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--cream);color:var(--ink);font-family:Jost,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
  h1,h2,h3,.serif{font-family:'Playfair Display',Georgia,serif}
  a{color:var(--green)} img,video{max-width:100%;display:block}
  .wrap{max-width:1140px;margin:0 auto;width:100%}.narrow{max-width:760px}
  nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 24px;background:rgba(250,245,233,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);overflow:hidden}
  nav::before{content:"";position:absolute;inset:0;opacity:.07;pointer-events:none;z-index:0;
    background-image:
      linear-gradient(30deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(150deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(30deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(150deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(60deg,var(--gold-dk) 25%,transparent 25.5%,transparent 75%,var(--gold-dk) 75%,var(--gold-dk)),
      linear-gradient(60deg,var(--gold-dk) 25%,transparent 25.5%,transparent 75%,var(--gold-dk) 75%,var(--gold-dk));
    background-size:36px 63px;
    background-position:0 0,0 0,18px 31.5px,18px 31.5px,0 0,18px 31.5px}
  nav .brand,nav .nav-actions{position:relative;z-index:1}
  .brand{display:flex;align-items:center;gap:11px;color:var(--green-dk);text-decoration:none;font-weight:700;letter-spacing:.06em;font-size:17px}
  .brand img{width:42px;height:42px;border-radius:50%;border:1px solid var(--gold)}
  .nav-actions{display:flex;align-items:center;gap:22px}
  .links{display:flex;align-items:center;gap:22px}.links a{color:var(--ink);text-decoration:none;font-size:13px;font-weight:600;letter-spacing:.03em}.links a:hover{color:var(--green)}
  .cart-link{position:relative;display:flex;align-items:center;gap:6px;color:var(--ink);text-decoration:none;font-size:13px;font-weight:600}
  .cart-badge{background:var(--gold);color:var(--green-dk);font-size:11px;font-weight:800;border-radius:999px;padding:1px 6px;min-width:16px;text-align:center}
  .menu-toggle{display:none;background:none;border:0;font-size:22px;line-height:1;cursor:pointer;color:var(--green-dk);padding:4px}
  .mobile-menu{display:none;flex-direction:column;position:sticky;top:69px;z-index:49;background:var(--paper);border-bottom:1px solid var(--line)}
  .mobile-menu.open{display:flex}
  .mobile-menu a{padding:15px 24px;border-top:1px solid var(--line);text-decoration:none;color:var(--ink);font-weight:600;font-size:14.5px}
  .btn{display:inline-flex;align-items:center;gap:8px;justify-content:center;border:0;border-radius:4px;background:var(--green);color:#fdfaf0;text-decoration:none;font-weight:600;padding:13px 26px;font-size:14px;letter-spacing:.03em;cursor:pointer}
  .btn:hover{background:var(--green-dk)}
  .btn.gold{position:relative;background:linear-gradient(135deg,var(--gold-lt),var(--gold) 50%,var(--gold-dk));color:#2a1c02;overflow:visible;box-shadow:0 6px 20px var(--gold-glow);text-shadow:0 1px 0 rgba(255,255,255,.25)}
  .btn.gold:hover{box-shadow:0 8px 30px var(--gold-glow),0 0 0 2px var(--gold-lt)}
  .btn.gold::after{content:"";position:absolute;left:50%;bottom:-7px;width:9px;height:9px;background:var(--gold);border-radius:50% 50% 50% 0;transform:translateX(-50%) rotate(45deg) scale(0);transition:transform .35s cubic-bezier(.34,1.56,.64,1);pointer-events:none}
  .btn.gold:hover::after{transform:translateX(-50%) translateY(4px) rotate(45deg) scale(1)}
  .btn.ghost{background:transparent;border:1px solid var(--green);color:var(--green)}
  .btn[disabled]{opacity:.5;cursor:not-allowed}
  .hero{position:relative;padding:0;display:grid;grid-template-columns:1fr 1fr;align-items:stretch;background:radial-gradient(1200px 600px at 15% 20%,#1c3a22,var(--green-dk) 60%);overflow:hidden;min-height:560px}
  .hero-copy{padding:70px 56px;display:flex;flex-direction:column;justify-content:center;color:#fdfaf0;position:relative;z-index:2}
  .hero-copy .eyebrow{color:var(--gold-lt);font-size:12px;letter-spacing:.28em;text-transform:uppercase;font-weight:600;text-shadow:0 0 16px var(--gold-glow)}
  .hero-copy h1{font-size:clamp(34px,4.6vw,54px);line-height:1.08;margin:16px 0 18px;color:#fff}
  .hero-copy h1 .g{background:linear-gradient(120deg,var(--gold-lt),var(--gold) 60%,var(--gold-dk));-webkit-background-clip:text;background-clip:text;color:transparent}
  .hero-copy p{font-size:17px;color:#e7e2d2;max-width:460px;margin:0 0 28px}
  .hero-img{position:relative}
  .hero-img::before{content:"";position:absolute;inset:-60px;background:radial-gradient(circle at 48% 42%,var(--gold-glow),transparent 62%);z-index:0;pointer-events:none;opacity:.8}
  .hero-img img{position:relative;z-index:1;width:100%;height:100%;object-fit:cover;min-height:340px}
  .marquee{position:relative;background:var(--green-dk);border-top:1px solid var(--gold-dk);border-bottom:1px solid var(--gold-dk);overflow:hidden}
  .marquee::before{content:"";position:absolute;inset:0;opacity:.28;pointer-events:none;z-index:0;
    background-image:
      linear-gradient(30deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(150deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(30deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(150deg,var(--gold) 12%,transparent 12.5%,transparent 87%,var(--gold) 87.5%,var(--gold)),
      linear-gradient(60deg,var(--gold-dk) 25%,transparent 25.5%,transparent 75%,var(--gold-dk) 75%,var(--gold-dk)),
      linear-gradient(60deg,var(--gold-dk) 25%,transparent 25.5%,transparent 75%,var(--gold-dk) 75%,var(--gold-dk));
    background-size:34px 60px;
    background-position:0 0,0 0,17px 30px,17px 30px,0 0,17px 30px}
  .marquee .track{position:relative;z-index:1;display:inline-flex;white-space:nowrap;animation:mq 30s linear infinite;padding:15px 0}
  .marquee:hover .track{animation-play-state:paused}
  .marquee .grp{display:inline-flex;align-items:center;font-weight:700;font-size:12.5px;color:var(--gold-lt);letter-spacing:.14em;text-transform:uppercase}
  .marquee .grp i{margin:0 22px;color:var(--gold-dk);font-style:normal}
  @keyframes mq{from{transform:translateX(0)}to{transform:translateX(-50%)}}
  .drip-row{position:relative;height:0;overflow:visible;z-index:6;pointer-events:none}
  .drip{position:absolute;top:0;width:18px;height:0}
  .drip::before{content:"";position:absolute;top:0;left:0;width:18px;border-radius:0 0 9px 9px;background:linear-gradient(105deg,var(--gold-dk) 0%,var(--gold) 30%,var(--gold-lt) 46%,var(--gold) 62%,var(--gold-dk) 100%);animation:dripStrand 3.6s ease-in infinite;box-shadow:0 1px 3px rgba(0,0,0,.18),inset 2px 0 3px rgba(255,255,255,.35)}
  .drip::after{content:"";position:absolute;left:1px;width:16px;height:16px;border-radius:50%;background:radial-gradient(circle at 35% 30%,var(--gold-lt),var(--gold) 55%,var(--gold-dk));animation:dripFall 3.6s ease-in infinite;opacity:0;box-shadow:0 2px 3px rgba(0,0,0,.15)}
  @keyframes dripStrand{0%,8%{height:0}55%{height:52px}63%{height:60px}66%{height:6px}100%{height:0}}
  @keyframes dripFall{0%,65%{top:0;opacity:0;transform:scale(.5)}66%{top:56px;opacity:1;transform:scale(1)}92%{top:150px;opacity:.85;transform:scale(.95)}100%{top:175px;opacity:0;transform:scale(.75)}}
  @media(prefers-reduced-motion:reduce){.drip::before{animation:none;height:22px}.drip::after{animation:none;opacity:0}}
  .band{padding:80px 24px}
  .head{text-align:center;max-width:640px;margin:0 auto 44px}
  .head .eyebrow{color:var(--gold);font-size:12px;letter-spacing:.24em;text-transform:uppercase;font-weight:700}
  .head .eyebrow::after{content:"";display:block;width:64px;height:2px;margin:14px auto 0;background:linear-gradient(90deg,transparent,var(--gold),transparent)}
  .reveal{opacity:0;transform:translateY(18px);transition:opacity .7s ease,transform .7s ease}.reveal.in{opacity:1;transform:none}
  .head h2{font-size:clamp(28px,3.6vw,40px);margin:12px 0}
  .head p{color:var(--mut);font-size:15.5px}
  .story-band{background:var(--paper);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .story-grid{display:grid;grid-template-columns:.9fr 1.1fr;gap:50px;align-items:center}
  .story-grid img{border-radius:6px}
  .video-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}
  .video-grid video{width:100%;border-radius:8px;background:#000;aspect-ratio:9/16;max-height:520px;object-fit:cover}
  .story-grid .copy p{color:#4a4234;font-size:15.5px;margin:0 0 16px}
  .products{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px}
  .pcard{position:relative;background:var(--paper);border:1px solid var(--line);border-radius:8px;overflow:hidden;text-decoration:none;color:var(--ink);display:block;transition:transform .25s,box-shadow .25s}
  .pcard:hover{transform:translateY(-4px);box-shadow:0 18px 40px rgba(31,58,36,.16),0 0 0 1px var(--gold)}
  .pcard::before{content:"";position:absolute;top:0;left:-60%;width:35%;height:100%;background:linear-gradient(120deg,transparent,rgba(255,255,255,.55),transparent);transform:skewX(-20deg);z-index:2;transition:left .8s ease;pointer-events:none}
  .pcard:hover::before{left:135%}
  .pcard img{aspect-ratio:4/5;object-fit:cover;width:100%}
  .pcard .body{padding:20px}
  .pcard h3{margin:0 0 6px;font-size:18px}
  .pcard .price{color:var(--green);font-weight:700;margin-top:8px}
  .pcard .tagline{color:var(--mut);font-size:13.5px;margin:0}
  .product-detail{display:grid;grid-template-columns:1fr 1fr;gap:56px;align-items:start}
  .gallery-main{border-radius:8px;overflow:hidden;border:1px solid var(--line);margin-bottom:12px}
  .gallery-main img{aspect-ratio:1/1;object-fit:cover;width:100%}
  .gallery-thumbs{display:flex;gap:10px}
  .gallery-thumbs img{width:74px;height:74px;object-fit:cover;border-radius:6px;border:1px solid var(--line);cursor:pointer}
  .gallery-thumbs img.active{border-color:var(--gold);border-width:2px}
  .pd-tagline{color:var(--mut);font-size:15px;margin:10px 0 22px}
  .bullets{list-style:none;padding:0;margin:0 0 26px;display:grid;gap:9px}
  .bullets li{font-size:14px;display:flex;gap:9px;align-items:flex-start}
  .field{margin:0 0 20px}
  .field label{display:block;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin-bottom:8px}
  .pillrow{display:flex;gap:10px;flex-wrap:wrap}
  .pill{border:1px solid var(--line);background:var(--paper);border-radius:999px;padding:9px 16px;font-size:13.5px;font-weight:600;cursor:pointer}
  .pill.active{background:var(--green);border-color:var(--green);color:#fff}
  .variety-blurb{background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:16px 18px;font-size:14px;color:#4a4234;margin-bottom:22px}
  .price-line{font-size:26px;font-weight:700;color:var(--green);margin:6px 0 22px;font-family:'Playfair Display',serif;text-shadow:0 0 20px var(--gold-glow)}
  .qty-row{display:flex;align-items:center;gap:14px;margin-bottom:22px}
  .qty-box{display:flex;align-items:center;border:1px solid var(--line);border-radius:4px}
  .qty-box button{border:0;background:none;width:38px;height:40px;font-size:16px;cursor:pointer}
  .qty-box input{width:44px;text-align:center;border:0;font-size:15px;font-family:inherit}
  .cart-table{width:100%;border-collapse:collapse}
  .cart-table td,.cart-table th{padding:14px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}
  .cart-table th{color:var(--mut);font-size:11px;letter-spacing:.08em;text-transform:uppercase;font-weight:700}
  .cart-thumb{width:56px;height:56px;object-fit:cover;border-radius:5px}
  .totals{max-width:340px;margin-left:auto;margin-top:24px}
  .totals .row{display:flex;justify-content:space-between;font-size:14.5px;padding:7px 0}
  .totals .row.total{font-size:19px;font-weight:700;color:var(--green);border-top:1px solid var(--line);margin-top:6px;padding-top:14px;font-family:'Playfair Display',serif}
  .empty-cart{text-align:center;padding:60px 20px;color:var(--mut)}
  .certbar{display:flex;justify-content:center;gap:14px;flex-wrap:wrap;margin:26px 0}
  .certbar span{background:var(--paper);border:1px solid var(--line);border-radius:999px;padding:8px 16px;font-size:12.5px;font-weight:700;color:var(--green-dk)}
  .prose p{margin:0 0 16px;font-size:15.5px;color:#4a4234}
  .prose h3{margin:30px 0 10px}
  .contact-form{display:grid;gap:16px;max-width:500px}
  .contact-form input,.contact-form textarea{font-family:inherit;font-size:15px;padding:12px 14px;border:1px solid var(--line);border-radius:5px;background:var(--paper)}
  .contact-form textarea{min-height:120px;resize:vertical}
  .msg{border-radius:6px;padding:12px 16px;font-size:14px;margin-top:14px}
  .msg.ok{background:#e9f2e6;border:1px solid #b9d6ac;color:#2c4b23}
  .msg.err{background:#f7e6e2;border:1px solid #e2b3a6;color:#7a2e1c}
  footer{padding:50px 24px 32px;text-align:center;color:#cfd6ce;background:var(--green-dk)}
  footer img{width:56px;margin:0 auto 14px;border-radius:50%;border:1px solid var(--gold)}
  footer a{color:var(--gold-lt)}
  footer .fine{margin-top:10px;font-size:12.5px;color:#9fae9c}
  @media(max-width:860px){
    .hero{grid-template-columns:1fr}.hero-img{order:-1;min-height:280px}.hero-copy{padding:48px 24px}
    .story-grid,.product-detail{grid-template-columns:1fr}
    .links{display:none}.menu-toggle{display:block}
    .band{padding:56px 18px}
  }
</style>
"""


def nav():
    count = cart_count()
    badge = f'<span class="cart-badge">{count}</span>' if count else ""
    return """
<nav>
  <a class="brand" href="/"><img src="/static/images/logo.jpg" alt="Mihai's Farm logo"><span>MIHAI&rsquo;S FARM</span></a>
  <div class="nav-actions">
    <div class="links">
      <a href="/shop">Shop</a><a href="/our-story">Our Story</a><a href="/delivery-returns">Delivery &amp; Returns</a><a href="/contact">Contact</a>
    </div>
    <a class="cart-link" href="/cart">🧺 Basket""" + badge + """</a>
    <button class="menu-toggle" onclick="document.getElementById('mobileMenu').classList.toggle('open')" aria-label="Menu">&#9776;</button>
  </div>
</nav>
<div class="mobile-menu" id="mobileMenu">
  <a href="/shop">Shop</a><a href="/our-story">Our Story</a><a href="/delivery-returns">Delivery &amp; Returns</a><a href="/contact">Contact</a>
</div>
"""

MARQUEE_GRP = ('<span class="grp">EU Certified Organic <i>&#10022;</i> Raw &amp; Unfiltered <i>&#10022;</i> '
               'Small Family Business <i>&#10022;</i> From the Carpathian Fields <i>&#10022;</i> '
               'QR-Verified Certification <i>&#10022;</i></span>')
MARQUEE = '<div class="marquee"><div class="track">' + MARQUEE_GRP + MARQUEE_GRP + '</div></div>'

DRIP_ROW = '<div class="drip-row">' + "".join(
    f'<span class="drip" style="left:{left}%;animation-delay:{delay}s"></span>'
    for left, delay in [(4, 0.2), (14, 2.4), (24, 1.1), (35, 3.0), (46, 0.6),
                         (57, 1.9), (68, 2.7), (78, 0.9), (88, 1.5), (95, 3.3)]
) + '</div>'

SCRIPTS = """
<script>
(function(){
  var els=document.querySelectorAll('.reveal');
  if(!('IntersectionObserver' in window)){els.forEach(function(e){e.classList.add('in')});return;}
  var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}})},{threshold:.12});
  els.forEach(function(e){io.observe(e)});
})();
</script>
"""


FOOTER = """
<footer>
  <img src="/static/images/logo.jpg" alt="Mihai's Farm logo">
  <div class="serif" style="font-size:18px;color:#fff">Mihai&rsquo;s Farm</div>
  <div style="margin-top:6px">Honest Food &middot; Traditional Values &middot; Natural Products</div>
  <div style="margin-top:12px"><a href="/shop">Shop</a> &nbsp;|&nbsp; <a href="/our-story">Our Story</a> &nbsp;|&nbsp; <a href="/delivery-returns">Delivery &amp; Returns</a> &nbsp;|&nbsp; <a href="/contact">Contact</a></div>
  <div class="fine">Inspired by Mihai &middot; From the Carpathian Fields</div>
</footer>
"""


def page(title, body, description="Organic, raw honey from the Carpathian fields."):
    return render_template_string(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>' + html.escape(title) + """ — Mihai's Farm</title>
<meta name="description" content=\"""" + html.escape(description) + """\">
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + BASE_STYLE + """</head><body>""" + nav() + body + FOOTER + SCRIPTS + "</body></html>"
    )


# --- Page bodies ----------------------------------------------------------------

def home_body():
    p = PRODUCTS[0]
    cheapest = min(v["price"] for v in p["variants"])
    return """
<header class="hero">
  <div class="hero-copy">
    <div class="eyebrow">Inspired by Mihai &middot; From the Carpathian Fields</div>
    <h1>Raw, organic honey<br>the <span class="g">honest</span> way.</h1>
    <p>Unfiltered, unheated and EU-certified organic, straight from the Carpathian fields. No shortcuts, no additives — just honey the way it should be.</p>
    <div style="display:flex;gap:14px;flex-wrap:wrap">
      <a class="btn gold" href="/shop">Shop the Honey</a>
      <a class="btn ghost" style="color:#fdfaf0;border-color:#fdfaf0" href="/our-story">Our Story</a>
    </div>
  </div>
  <div class="hero-img"><img src="/static/images/product/jar-field.jpg" alt="Jar of Mihai's Farm honey in a wildflower field"></div>
</header>
""" + MARQUEE + DRIP_ROW + """

<section class="band"><div class="wrap">
  <div class="head reveal"><div class="eyebrow">Featured</div><h2>""" + html.escape(p["name"]) + """</h2><p>""" + html.escape(p["tagline"]) + """</p></div>
  <div class="products reveal" style="max-width:380px;margin:0 auto">
    <a class="pcard" href="/product/""" + p["slug"] + """">
      <img src="/static/images/""" + p["images"][0] + """" alt=\"""" + html.escape(p["name"]) + """\">
      <div class="body"><h3>""" + html.escape(p["name"]) + """</h3><p class="tagline">Acacia &amp; Rapeseed &middot; 350g / 900g</p><p class="price">From """ + gbp(cheapest) + """</p></div>
    </a>
  </div>
</div></section>

<section class="band story-band"><div class="wrap story-grid reveal">
  <img src="/static/images/product/bees-on-comb.jpg" alt="Bees at work on the honeycomb">
  <div class="copy">
    <div class="eyebrow" style="color:var(--gold);text-align:left">Our Story</div>
    <h2 class="serif" style="font-size:30px;margin:10px 0 16px">A small family business, inspired by Mihai.</h2>
    <p>The name Mihai&rsquo;s Farm was chosen in honour of Mihai, whose values of hard work, simplicity and appreciation for honest food continue to inspire everything we do.</p>
    <p>We believe great products don&rsquo;t need complicated ingredients or shortcuts — only care, patience and respect for the natural world.</p>
    <a class="btn ghost" href="/our-story">Read the full story</a>
  </div>
</div></section>
"""


def shop_body():
    cards = ""
    for p in PRODUCTS:
        cheapest = min(v["price"] for v in p["variants"])
        cards += (
            '<a class="pcard" href="/product/' + p["slug"] + '">'
            '<img src="/static/images/' + p["images"][0] + '" alt="' + html.escape(p["name"]) + '">'
            '<div class="body"><h3>' + html.escape(p["name"]) + '</h3>'
            '<p class="tagline">' + html.escape(p["tagline"]) + '</p>'
            '<p class="price">From ' + gbp(cheapest) + '</p></div></a>'
        )
    return """
<section class="band"><div class="wrap">
  <div class="head reveal"><div class="eyebrow">Shop</div><h2>Our honey</h2><p>More farm products are on the way — this is just the beginning.</p></div>
  <div class="products reveal">""" + cards + """</div>
</div></section>
"""


def product_body(p):
    variants_json_bits = []
    for v in p["variants"]:
        variants_json_bits.append(
            '{id:"' + v["id"] + '",variety:"' + v["variety"] + '",size:"' + v["size"] + '",price:' + str(v["price"]) + '}'
        )
    variants_js = "[" + ",".join(variants_json_bits) + "]"

    varieties_html = ""
    for i, var in enumerate(p["varieties"]):
        active = " active" if i == 0 else ""
        varieties_html += f'<button type="button" class="pill{active}" data-variety="{var["key"]}" onclick="pickVariety(\'{var["key"]}\')">{var["emoji"]} {html.escape(var["label"])}</button>'

    blurbs_html = ""
    for i, var in enumerate(p["varieties"]):
        display = "block" if i == 0 else "none"
        blurbs_html += f'<div class="variety-blurb" data-blurb="{var["key"]}" style="display:{display}">{html.escape(var["blurb"])}</div>'

    sizes = sorted({v["size"] for v in p["variants"]}, key=lambda s: int(s.replace("g", "")))
    sizes_html = "".join(
        f'<button type="button" class="pill{" active" if i == 0 else ""}" data-size="{s}" onclick="pickSize(\'{s}\')">{s}</button>'
        for i, s in enumerate(sizes)
    )

    bullets_html = "".join(f'<li>{emoji} {html.escape(text)}</li>' for emoji, text in p["bullets"])
    thumbs_html = "".join(
        f'<img src="/static/images/{img}" class="{"active" if i == 0 else ""}" onclick="setMain(\'{img}\', this)">'
        for i, img in enumerate(p["images"])
    )

    return """
<section class="band"><div class="wrap product-detail">
  <div>
    <div class="gallery-main"><img id="mainImg" src="/static/images/""" + p["images"][0] + """" alt=\"""" + html.escape(p["name"]) + """\"></div>
    <div class="gallery-thumbs">""" + thumbs_html + """</div>
  </div>
  <div>
    <div class="eyebrow" style="color:var(--gold)">Mihai&rsquo;s Farm</div>
    <h1 class="serif" style="font-size:32px;margin:8px 0 4px">""" + html.escape(p["name"]) + """</h1>
    <p class="pd-tagline">""" + html.escape(p["tagline"]) + """</p>

    <div class="field"><label>Variety</label><div class="pillrow">""" + varieties_html + """</div></div>
    """ + blurbs_html + """
    <div class="field"><label>Size</label><div class="pillrow">""" + sizes_html + """</div></div>

    <div class="price-line" id="priceLine">—</div>

    <div class="qty-row">
      <div class="qty-box">
        <button type="button" onclick="stepQty(-1)">−</button>
        <input id="qtyInput" type="text" value="1" readonly>
        <button type="button" onclick="stepQty(1)">+</button>
      </div>
      <button class="btn gold" id="addBtn" onclick="addToCart()">Add to Basket</button>
    </div>
    <div id="addMsg"></div>

    <ul class="bullets">""" + bullets_html + """</ul>

    <p style="font-size:14.5px;color:#4a4234">""" + html.escape(p["description"]) + """</p>
    <p style="font-size:13px;color:var(--mut);margin-top:18px"><strong>Storage:</strong> """ + html.escape(p["storage"]) + """</p>
  </div>
</div></section>

<script>
var VARIANTS = """ + variants_js + """;
var state = {variety: VARIANTS[0].variety, size: VARIANTS[0].size, qty: 1};

function findVariant(){
  return VARIANTS.find(function(v){ return v.variety === state.variety && v.size === state.size; });
}
function renderPrice(){
  var v = findVariant();
  document.getElementById('priceLine').textContent = v ? '£' + (v.price/100).toFixed(2) : 'Not available';
}
function pickVariety(key){
  state.variety = key;
  document.querySelectorAll('[data-variety]').forEach(function(b){ b.classList.toggle('active', b.dataset.variety === key); });
  document.querySelectorAll('[data-blurb]').forEach(function(b){ b.style.display = (b.dataset.blurb === key) ? 'block' : 'none'; });
  renderPrice();
}
function pickSize(size){
  state.size = size;
  document.querySelectorAll('[data-size]').forEach(function(b){ b.classList.toggle('active', b.dataset.size === size); });
  renderPrice();
}
function stepQty(delta){
  state.qty = Math.max(1, Math.min(20, state.qty + delta));
  document.getElementById('qtyInput').value = state.qty;
}
function setMain(src, el){
  document.getElementById('mainImg').src = '/static/images/' + src;
  document.querySelectorAll('.gallery-thumbs img').forEach(function(i){ i.classList.remove('active'); });
  el.classList.add('active');
}
async function addToCart(){
  var v = findVariant();
  if(!v) return;
  var btn = document.getElementById('addBtn');
  btn.disabled = true; btn.textContent = 'Adding...';
  try{
    var r = await fetch('/cart/add', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({variant_id: v.id, qty: state.qty})});
    var d = await r.json();
    var msg = document.getElementById('addMsg');
    if(d.ok){
      msg.className = 'msg ok';
      msg.innerHTML = 'Added to your basket. <a href="/cart">View basket &rarr;</a>';
    } else {
      msg.className = 'msg err'; msg.textContent = d.error || 'Could not add to basket.';
    }
  } catch(e){
    document.getElementById('addMsg').className = 'msg err';
    document.getElementById('addMsg').textContent = 'Something went wrong — please try again.';
  }
  btn.disabled = false; btn.textContent = 'Add to Basket';
}
renderPrice();
</script>
"""


def cart_body(error=None):
    lines = cart_lines()
    if not lines:
        return """
<section class="band"><div class="wrap narrow">
  <div class="empty-cart"><h2 class="serif">Your basket is empty</h2><p>Nothing here yet.</p><a class="btn gold" href="/shop">Shop the Honey</a></div>
</div></section>
"""
    rows = ""
    for l in lines:
        rows += (
            '<tr><td><div style="display:flex;align-items:center;gap:12px">'
            '<img class="cart-thumb" src="/static/images/' + l["product"]["images"][0] + '" alt="">'
            '<div><div style="font-weight:600">' + html.escape(l["variant"]["label"]) + '</div>'
            '<a href="/cart/remove/' + l["variant"]["id"] + '" style="font-size:12px;color:#a24">Remove</a></div></div></td>'
            '<td>' + gbp(l["variant"]["price"]) + '</td>'
            '<td>'
            '<form method="post" action="/cart/update" style="display:flex;gap:6px;align-items:center">'
            '<input type="hidden" name="variant_id" value="' + l["variant"]["id"] + '">'
            '<input type="number" name="qty" value="' + str(l["qty"]) + '" min="0" max="20" style="width:56px;padding:6px;border:1px solid var(--line);border-radius:4px">'
            '<button class="btn ghost" type="submit" style="padding:8px 14px;font-size:12px">Update</button>'
            '</form></td>'
            '<td>' + gbp(l["line_total"]) + '</td></tr>'
        )
    subtotal = cart_subtotal()
    error_html = f'<div class="msg err">{html.escape(error)}</div>' if error else ""
    return """
<section class="band"><div class="wrap narrow">
  <div class="head" style="margin-bottom:26px"><h2>Your Basket</h2></div>
  <table class="cart-table">
    <tr><th>Item</th><th>Price</th><th>Quantity</th><th>Total</th></tr>
    """ + rows + """
  </table>
  <div class="totals">
    <div class="row"><span>Subtotal</span><span>""" + gbp(subtotal) + """</span></div>
    <div class="row"><span>UK Postage</span><span>""" + gbp(SHIPPING_PENCE) + """</span></div>
    <div class="row total"><span>Total</span><span>""" + gbp(subtotal + SHIPPING_PENCE) + """</span></div>
  </div>
  """ + error_html + """
  <form method="post" action="/checkout" style="margin-top:22px;text-align:right">
    <button class="btn gold" type="submit">Checkout securely &rarr;</button>
  </form>
</div></section>
"""


def our_story_body():
    return """
<section class="band"><div class="wrap narrow prose">
  <div class="head reveal"><div class="eyebrow">Our Story</div><h2>Inspired by tradition, nature and the simple beauty of honest food.</h2></div>
  <img src="/static/images/banner.jpg" alt="Mihai's Farm" style="border-radius:8px;margin-bottom:32px">
  <p>Welcome to Mihai&rsquo;s Farm.</p>
  <p>Our journey began with a simple idea: to share genuine products made with respect for nature, tradition and quality.</p>
  <p>The name Mihai&rsquo;s Farm was chosen in honour of Mihai, whose values of hard work, simplicity and appreciation for honest food continue to inspire everything we do.</p>
  <p>We believe that great products do not need complicated ingredients or shortcuts — only care, patience and respect for the natural world.</p>
  <p>Today, our focus is premium organic honey, carefully produced and selected to preserve its natural flavour, character and quality. Every jar reflects our commitment to authenticity and transparency, including independently verifiable organic certification.</p>
  <p>As our journey continues, we hope to expand our range with other carefully selected farm products that share the same values of quality, tradition and respect for nature.</p>
  <p>Thank you for supporting a small family business and for becoming part of our story.</p>
  <div class="certbar reveal">
    <span>🌱 EU Certified Organic</span><span>🍯 Raw &amp; Unfiltered</span><span>📱 QR-Verified</span>
  </div>
</div></section>

<section class="band story-band"><div class="wrap">
  <div class="head reveal"><div class="eyebrow">Straight From the Hives</div><h2>See it for yourself</h2><p>A closer look at the honey, straight from harvest.</p></div>
  <div class="video-grid reveal">
    <video controls preload="metadata" playsinline src="/static/videos/harvest-1.mp4"></video>
    <video controls preload="metadata" playsinline src="/static/videos/harvest-2.mp4"></video>
  </div>
</div></section>
"""


def delivery_returns_body():
    return """
<section class="band"><div class="wrap narrow prose">
  <div class="head reveal"><div class="eyebrow">Delivery &amp; Returns</div><h2>Straightforward, honestly written.</h2></div>
  <h3>Delivery</h3>
  <p>We currently deliver within the United Kingdom. Postage is a flat """ + gbp(SHIPPING_PENCE) + """ per order, and orders are usually with you within a week of dispatch.</p>
  <h3>Returns</h3>
  <p>Because honey is a natural food product, we're unable to accept returns once a jar has been opened. If your order arrives damaged, faulty, or isn't what you ordered, contact us within 48 hours of delivery and we'll sort a replacement or refund.</p>
  <h3>Storage</h3>
  <p>Store your honey in a cool, dry place away from direct sunlight. Natural crystallisation is a sign of genuine, minimally processed honey and may occur over time — it doesn't mean anything is wrong with it.</p>
</div></section>
"""


def contact_body(sent=False, error=None):
    if sent:
        inner = '<div class="msg ok">Thanks — your message is on its way to us. We\'ll reply as soon as we can.</div>'
    else:
        error_html = f'<div class="msg err">{html.escape(error)}</div>' if error else ""
        inner = """
    <form class="contact-form" method="post" action="/contact">
      <input type="text" name="website" autocomplete="off" tabindex="-1" style="position:absolute;left:-9999px" aria-hidden="true">
      <input type="text" name="name" placeholder="Your name" required>
      <input type="email" name="email" placeholder="Your email" required>
      <textarea name="message" placeholder="How can we help?" required></textarea>
      <button class="btn gold" type="submit">Send Message</button>
    </form>
    """ + error_html
    return """
<section class="band"><div class="wrap narrow">
  <div class="head reveal"><div class="eyebrow">Contact</div><h2>Get in touch</h2><p>Questions about an order, or about the honey itself — we're happy to help.</p></div>
  """ + inner + """
</div></section>
"""


def order_success_body(order_summary=None):
    summary_html = ""
    if order_summary:
        rows = "".join(
            f'<div class="totals row"><span>{html.escape(li["name"])} &times; {li["qty"]}</span><span>{gbp(li["amount"])}</span></div>'
            for li in order_summary["items"]
        )
        summary_html = f'<div style="max-width:420px;margin:26px auto 0;text-align:left">{rows}<div class="totals row total"><span>Total</span><span>{gbp(order_summary["total"])}</span></div></div>'
    return """
<section class="band"><div class="wrap narrow" style="text-align:center">
  <div class="eyebrow" style="color:var(--gold)">Thank you</div>
  <h2 class="serif">Your order is confirmed 🍯</h2>
  <p style="color:var(--mut)">We're packing it with care — a confirmation email is on its way to you.</p>
  """ + summary_html + """
  <div style="margin-top:30px"><a class="btn gold" href="/shop">Continue Shopping</a></div>
</div></section>
"""


def order_cancelled_body():
    return """
<section class="band"><div class="wrap narrow" style="text-align:center">
  <h2 class="serif">Checkout cancelled</h2>
  <p style="color:var(--mut)">No worries — your basket is still waiting for you.</p>
  <div style="margin-top:24px"><a class="btn gold" href="/cart">Back to Basket</a></div>
</div></section>
"""


# --- Routes -----------------------------------------------------------------

@app.route("/")
def home():
    return page("Home", home_body(), "Raw, unfiltered, EU-certified organic honey from the Carpathian fields.")


@app.route("/shop")
def shop():
    return page("Shop", shop_body())


@app.route("/product/<slug>")
def product(slug):
    p = find_product(slug)
    if not p:
        return page("Not found", "<section class='band'><div class='wrap narrow'><h2>Product not found</h2></div></section>"), 404
    return page(p["name"], product_body(p), p["tagline"])


@app.route("/cart")
def cart_view():
    return page("Your Basket", cart_body())


@app.route("/cart/add", methods=["POST"])
def cart_add():
    data = request.get_json(silent=True) or {}
    variant_id = data.get("variant_id")
    qty = max(1, min(20, int(data.get("qty") or 1)))
    product, variant = find_variant(variant_id)
    if not variant:
        return jsonify({"ok": False, "error": "That option isn't available."}), 400
    cart = session.get("cart", {})
    cart[variant_id] = min(20, cart.get(variant_id, 0) + qty)
    session["cart"] = cart
    return jsonify({"ok": True, "count": cart_count()})


@app.route("/cart/update", methods=["POST"])
def cart_update():
    variant_id = request.form.get("variant_id")
    qty = max(0, min(20, int(request.form.get("qty") or 0)))
    cart = session.get("cart", {})
    if qty == 0:
        cart.pop(variant_id, None)
    else:
        cart[variant_id] = qty
    session["cart"] = cart
    return redirect(url_for("cart_view"))


@app.route("/cart/remove/<variant_id>")
def cart_remove(variant_id):
    cart = session.get("cart", {})
    cart.pop(variant_id, None)
    session["cart"] = cart
    return redirect(url_for("cart_view"))


@app.route("/checkout", methods=["POST"])
def checkout():
    lines = cart_lines()
    if not lines:
        return redirect(url_for("cart_view"))
    if not stripe or not os.environ.get("STRIPE_SECRET_KEY"):
        return page("Your Basket", cart_body(error="Online payments aren't switched on yet — please check back shortly or contact us to place your order directly."))

    line_items = [
        {
            "price_data": {
                "currency": "gbp",
                "product_data": {"name": SHOP_NAME + " — " + l["variant"]["label"]},
                "unit_amount": l["variant"]["price"],
            },
            "quantity": l["qty"],
        }
        for l in lines
    ]
    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            shipping_address_collection={"allowed_countries": ["GB"]},
            shipping_options=[{
                "shipping_rate_data": {
                    "type": "fixed_amount",
                    "fixed_amount": {"amount": SHIPPING_PENCE, "currency": "gbp"},
                    "display_name": "UK Postage",
                    "delivery_estimate": {"minimum": {"unit": "business_day", "value": 3}, "maximum": {"unit": "business_day", "value": 7}},
                },
            }],
            success_url=url_for("order_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("order_cancelled", _external=True),
        )
    except Exception as e:
        print(f"Stripe checkout session failed: {e}")
        return page("Your Basket", cart_body(error="Something went wrong starting checkout — please try again."))

    return redirect(checkout_session.url, code=303)


@app.route("/order/success")
def order_success():
    session_id = request.args.get("session_id")
    summary = None
    if session_id and stripe and os.environ.get("STRIPE_SECRET_KEY"):
        try:
            cs = stripe.checkout.Session.retrieve(session_id)
            items = stripe.checkout.Session.list_line_items(session_id, limit=100)
            summary = {
                "total": cs.get("amount_total", 0),
                "items": [{"name": it["description"], "qty": it["quantity"], "amount": it["amount_total"]} for it in items.get("data", [])],
            }
        except Exception as e:
            print(f"Could not load order summary: {e}")
    session["cart"] = {}
    return page("Order Confirmed", order_success_body(summary))


@app.route("/order/cancelled")
def order_cancelled():
    return page("Checkout Cancelled", order_cancelled_body())


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        return "", 400
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print(f"Webhook signature check failed: {e}")
        return "", 400

    if event["type"] == "checkout.session.completed":
        send_order_emails(event["data"]["object"])

    return "", 200


@app.route("/our-story")
def our_story():
    return page("Our Story", our_story_body())


@app.route("/delivery-returns")
def delivery_returns():
    return page("Delivery & Returns", delivery_returns_body())


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "GET":
        return page("Contact", contact_body())
    if (request.form.get("website") or "").strip():
        return page("Contact", contact_body(sent=True))
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    message = (request.form.get("message") or "").strip()
    if not name or not email or not message:
        return page("Contact", contact_body(error="Please fill in every field."))
    send_contact_email(name, email, message)
    return page("Contact", contact_body(sent=True))


@app.route("/sitemap.xml")
def sitemap():
    pages = ["/", "/shop", "/our-story", "/delivery-returns", "/contact"] + [f"/product/{p['slug']}" for p in PRODUCTS]
    base = "https://mihaisfarm.co.uk"
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in pages)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: https://mihaisfarm.co.uk/sitemap.xml", mimetype="text/plain")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
