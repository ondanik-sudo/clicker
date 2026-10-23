# Картинки для Telegram-бота в стиле SugarClick: тёмное стекло, сине-фиолетовый градиент, арки планет, сахарный кубик.
# Всё рисуется в 2x и уменьшается - так у кругов и скруглений гладкие края.
import io
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
# шрифт ищем и в папке fonts/, и рядом с файлом (на GitHub его могли загрузить без папки)
FONT_FILE = next((p for p in (os.path.join(_HERE, 'fonts', 'Manrope.ttf'), os.path.join(_HERE, 'Manrope.ttf')) if os.path.exists(p)),
                 os.path.join(_HERE, 'fonts', 'Manrope.ttf'))
S = 2  # коэффициент суперсэмплинга

A1, A2 = (59, 99, 255), (138, 61, 255)  # акцентные цвета программы (--a1, --a2)
NAVY, DEEP = (16, 22, 58), (40, 18, 92)
WHITE = (255, 255, 255)
SOFT = (205, 214, 255)
GREEN, ORANGE, RED, GREY = (110, 231, 160), (255, 170, 80), (255, 110, 110), (150, 160, 190)

_fonts = {}


def font(size, weight=500):
    k = (size, weight)
    if k not in _fonts:
        f = ImageFont.truetype(FONT_FILE, size * S)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        _fonts[k] = f
    return _fonts[k]


def gradient(w, h, tl, tr, bl, br):
    """Плавный градиент по четырём углам: 2x2 картинка, растянутая билинейно."""
    small = Image.new('RGB', (2, 2))
    small.putdata([tl, tr, bl, br])
    return small.resize((w, h), Image.BILINEAR)


def glow(img, spots):
    """Мягкие светящиеся пятна (x, y, радиус, цвет, прозрачность) - рисуются в низком разрешении и размываются."""
    w, h = img.size
    k = 8
    layer = Image.new('RGBA', (w // k, h // k), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x, y, r, color, a in spots:
        d.ellipse([(x - r) * S / k, (y - r) * S / k, (x + r) * S / k, (y + r) * S / k], fill=color + (a,))
    layer = layer.filter(ImageFilter.GaussianBlur(14)).resize((w, h), Image.BILINEAR)
    img.alpha_composite(layer)


def overlay(img):
    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    return layer, ImageDraw.Draw(layer)


def arcs(img, items):
    """Арки планет: (центр x, центр y, радиус, цвет, прозрачность, толщина)."""
    layer, d = overlay(img)
    for x, y, r, color, a, wdt in items:
        d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], outline=color + (a,), width=wdt * S)
    img.alpha_composite(layer)


def glass(img, box, radius=36, fill=24, line=70):
    layer, d = overlay(img)
    x0, y0, x1, y1 = box
    d.rounded_rectangle([x0 * S, y0 * S, x1 * S, y1 * S], radius * S, fill=(255, 255, 255, fill), outline=(255, 255, 255, line), width=2 * S)
    # блик по верхнему краю стекла
    d.rounded_rectangle([(x0 + radius) * S, (y0 + 3) * S, (x1 - radius) * S, (y0 + 7) * S], 2 * S, fill=(255, 255, 255, 22))
    img.alpha_composite(layer)


def text(img, xy, s, size, weight=500, color=WHITE, anchor='la', alpha=255):
    layer, d = overlay(img)
    d.text((xy[0] * S, xy[1] * S), s, font=font(size, weight), fill=color + (alpha,), anchor=anchor)
    img.alpha_composite(layer)


def text_w(s, size, weight=500):
    return font(size, weight).getlength(s) / S


def pill(img, x, y, s, color, size=22, anchor_right=False):
    w = text_w(s, size, 600) + 44
    if anchor_right:
        x -= w
    layer, d = overlay(img)
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + size + 22) * S], (size + 22) * S // 2, fill=color + (55,), outline=color + (120,), width=2 * S)
    d.ellipse([(x + 16) * S, (y + (size + 22) / 2 - 5) * S, (x + 26) * S, (y + (size + 22) / 2 + 5) * S], fill=color + (255,))
    img.alpha_composite(layer)
    text(img, (x + 34, y + (size + 22) / 2), s, size, 600, color=tuple(min(255, c + 60) for c in color), anchor='lm')
    return w


def cube(img, cx, cy, s, orbit=True):
    """Изометрический сахарный кубик с орбитой кристаллов."""
    layer, d = overlay(img)
    P = lambda pts: [(x * S, y * S) for x, y in pts]
    if orbit:
        d.ellipse([(cx - s * 2.1) * S, (cy - s * 0.55) * S, (cx + s * 2.1) * S, (cy + s * 0.95) * S], outline=(200, 210, 255, 70), width=2 * S)
    top = [(cx, cy - s), (cx + s * 0.92, cy - s * 0.5), (cx, cy), (cx - s * 0.92, cy - s * 0.5)]
    left = [(cx - s * 0.92, cy - s * 0.5), (cx, cy), (cx, cy + s * 1.05), (cx - s * 0.92, cy + s * 0.55)]
    right = [(cx + s * 0.92, cy - s * 0.5), (cx, cy), (cx, cy + s * 1.05), (cx + s * 0.92, cy + s * 0.55)]
    d.polygon(P(left), fill=(196, 206, 255, 255))
    d.polygon(P(right), fill=(150, 165, 245, 255))
    d.polygon(P(top), fill=(255, 255, 255, 255))
    # сахарные крупинки на гранях
    for fx, fy in ((-0.5, 0.15), (-0.3, 0.55), (-0.62, 0.42), (0.45, 0.2), (0.3, 0.6), (0.62, 0.35), (0.1, -0.55), (-0.25, -0.45), (0.3, -0.5)):
        r = s * 0.035
        d.ellipse([(cx + fx * s - r) * S, (cy + fy * s - r) * S, (cx + fx * s + r) * S, (cy + fy * s + r) * S], fill=(255, 255, 255, 150))
    d.line(P([(cx, cy), (cx, cy + s * 1.05)]), fill=(255, 255, 255, 120), width=2 * S)
    if orbit:
        for ang, r, col in ((200, 0.09, (191, 225, 255)), (330, 0.07, (227, 201, 255)), (20, 0.06, (255, 255, 255)), (120, 0.08, (170, 190, 255))):
            a = math.radians(ang)
            x, y = cx + math.cos(a) * s * 2.1, cy + s * 0.2 + math.sin(a) * s * 0.75
            rr = s * r
            d.polygon(P([(x, y - rr * 1.4), (x + rr, y), (x, y + rr * 1.4), (x - rr, y)]), fill=col + (235,))
    img.alpha_composite(layer)


def ring(img, cx, cy, r, frac, color, width=26):
    layer, d = overlay(img)
    box = [(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S]
    d.ellipse(box, outline=(255, 255, 255, 30), width=width * S)
    if frac > 0:
        end = -90 + 360 * max(0.02, min(1.0, frac))
        d.arc(box, -90, end, fill=color + (255,), width=width * S)
        # скруглённые концы дуги
        for ang in (-90, end):
            a = math.radians(ang)
            x, y = cx + math.cos(a) * (r - width / 2), cy + math.sin(a) * (r - width / 2)
            d.ellipse([(x - width / 2) * S, (y - width / 2) * S, (x + width / 2) * S, (y + width / 2) * S], fill=color + (255,))
    img.alpha_composite(layer)


def canvas(w, h, tl, tr, bl, br):
    return gradient(w * S, h * S, tl, tr, bl, br).convert('RGBA')


def finish(img):
    w, h = img.size
    out = img.resize((w // S, h // S), Image.LANCZOS).convert('RGB')
    buf = io.BytesIO()
    out.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def fit(s, size, weight, max_w):
    """Уменьшает шрифт, пока строка не влезет в ширину (текст не должен вылезать за рамку)."""
    while size > 14 and text_w(s, size, weight) > max_w:
        size -= 2
    return size


# ===== Готовые карточки =====
def banner(status=None):
    """Главное меню. status: 'on' (подписка активна), 'warn' (меньше 3 часов), 'off' (нет подписки) или None."""
    W, H = 1280, 640
    img = canvas(W, H, (27, 42, 138), (52, 26, 120), (20, 24, 80), (74, 26, 143))
    glow(img, [(1000, 300, 260, A2, 150), (200, 120, 220, A1, 120), (700, 640, 200, (90, 60, 220), 90)])
    arcs(img, [(1220, 820, 520, A2, 110, 4), (1220, 820, 400, A1, 130, 3), (-60, -120, 360, (111, 139, 255), 80, 2)])
    glass(img, (70, 100, 1210, 540), 48)
    cube(img, 960, 300, 120)
    text(img, (136, 290), 'SugarClick', 96, 800, anchor='ls')
    text(img, (142, 352), 'автокликер для «Номерка»', 36, 500, SOFT, anchor='ls')
    if status:
        label, color = {'on': ('подписка активна', GREEN), 'warn': ('подписка заканчивается', ORANGE),
                        'off': ('нет активной подписки', GREY)}[status]
        pill(img, 142, 396, label, color, 26)
    return finish(img)


def plan_card(title, subtitle, price, old_price=None, badge=None):
    W, H = 1280, 520
    img = canvas(W, H, A1, A2, (40, 70, 220), (110, 40, 220))
    glow(img, [(1100, 80, 260, (255, 255, 255), 50), (100, 500, 240, (20, 30, 120), 120)])
    arcs(img, [(1180, 60, 300, WHITE, 60, 3), (1180, 60, 200, WHITE, 40, 2), (-80, 560, 260, WHITE, 45, 2)])
    glass(img, (50, 50, 1230, 470), 44, fill=18, line=80)
    text(img, (110, 120), 'Тариф', 32, 500, (228, 232, 255))
    size = fit(title, 96, 800, 640)
    text(img, (106, 240), title, size, 800, anchor='ls')
    text(img, (110, 300), subtitle, fit(subtitle, 32, 500, 560), 500, (228, 232, 255), anchor='ls')
    price_s = f'{price} ₽'
    text(img, (1170, 330), price_s, 104, 800, anchor='rs')
    if old_price and old_price != price:
        old_s = f'{old_price} ₽'
        x1 = 1170 - text_w(price_s, 104, 800) - 30
        text(img, (x1, 330), old_s, 44, 600, anchor='rs', alpha=150)
        layer, d = overlay(img)
        ow = text_w(old_s, 44, 600)
        d.line([((x1 - ow) * S, 314 * S), (x1 * S, 314 * S)], fill=(255, 255, 255, 190), width=3 * S)
        img.alpha_composite(layer)
    if badge:
        pill(img, 110, 332, badge, (255, 220, 120) if badge.startswith('Подарок') else GREEN, 26)
    cube(img, 1060, 150, 46, orbit=False)
    return finish(img)


def status_card(left, plan, until, frac, state, label=None):
    """Моя подписка: кольцо с остатком времени. state: on / warn / off; label - своя надпись на плашке."""
    W, H = 1280, 600
    img = canvas(W, H, (20, 28, 72), (30, 22, 70), (14, 18, 48), (34, 20, 84))
    glow(img, [(280, 300, 260, A1, 110), (1150, 120, 220, A2, 100)])
    arcs(img, [(-40, 660, 460, A1, 100, 3), (1300, -80, 340, A2, 90, 2)])
    color = {'on': (111, 139, 255), 'warn': ORANGE, 'off': GREY}[state]
    ring(img, 300, 300, 190, frac, color, 30)
    size = fit(left, 64, 800, 300)
    text(img, (300, 300), left, size, 800, anchor='mm')
    text(img, (300, 360), 'осталось' if state != 'off' else 'нет подписки', 28, 500, (170, 182, 232), anchor='mm')
    glass(img, (580, 130, 1220, 470), 40)
    text(img, (630, 230), plan, fit(plan, 64, 800, 560), 800, anchor='ls')
    text(img, (632, 300), until, fit(until, 30, 500, 560), 500, (170, 182, 232), anchor='ls')
    default, pc = {'on': ('подписка активна', GREEN), 'warn': ('скоро закончится', ORANGE), 'off': ('купите или продлите', GREY)}[state]
    pill(img, 632, 340, label or default, pc, 24)
    return finish(img)


def invite_card(bonus='+1 день вам', sub1='за каждую оплату друга', sub2='а другу −10% на первую покупку'):
    W, H = 1280, 560
    img = canvas(W, H, (42, 29, 122), (24, 40, 120), (30, 20, 90), (20, 48, 110))
    glow(img, [(1000, 280, 260, A2, 130), (160, 80, 200, A1, 100)])
    arcs(img, [(1260, 600, 420, A1, 90, 3), (-100, -60, 300, A2, 80, 2)])
    layer, d = overlay(img)
    for x, y, r in ((960, 280, 120), (1130, 180, 64), (1110, 410, 54), (820, 140, 40)):
        d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=(255, 255, 255, 26), outline=(255, 255, 255, 80), width=2 * S)
    d.line([(1030 * S, 230 * S), (1080 * S, 200 * S)], fill=(255, 255, 255, 70), width=2 * S)
    d.line([(1030 * S, 340 * S), (1065 * S, 380 * S)], fill=(255, 255, 255, 70), width=2 * S)
    img.alpha_composite(layer)
    text(img, (960, 280), '+1', 72, 800, anchor='mm')
    text(img, (100, 230), bonus, fit(bonus, 80, 800, 640), 800, anchor='ls')
    text(img, (104, 310), sub1, 36, 500, SOFT, anchor='ls')
    text(img, (104, 370), sub2, 36, 500, SOFT, anchor='ls')
    return finish(img)


def gift_card(sender, plan, key, wish=''):
    W, H = 1280, 720
    img = canvas(W, H, A1, A2, (60, 60, 230), (120, 40, 210))
    glow(img, [(1100, 100, 280, (255, 255, 255), 60), (150, 650, 260, (30, 20, 120), 130)])
    arcs(img, [(1280, 0, 400, WHITE, 60, 3), (0, 720, 320, WHITE, 60, 3), (1280, 0, 300, WHITE, 35, 2)])
    glass(img, (70, 70, 1210, 650), 52, fill=26, line=100)
    text(img, (140, 170), f'Подарок от {sender}', fit(f'Подарок от {sender}', 36, 600, 800), 600, (228, 232, 255), anchor='ls')
    title = f'SugarClick · {plan}'
    text(img, (136, 270), title, fit(title, 80, 800, 820), 800, anchor='ls')
    layer, d = overlay(img)
    d.rounded_rectangle([140 * S, 320 * S, 1140 * S, 450 * S], 28 * S, fill=(14, 20, 48, 140), outline=(255, 255, 255, 60), width=2 * S)
    img.alpha_composite(layer)
    text(img, (640, 386), key, 64, 700, anchor='mm')
    if wish:
        w = f'«{wish}»'
        text(img, (140, 540), w, fit(w, 36, 500, 1000), 500, (228, 232, 255), anchor='ls')
    text(img, (140, 600), 'Активируйте ключ в окне активации SugarClick', 26, 500, (228, 232, 255), anchor='ls', alpha=190)
    cube(img, 1080, 175, 56, orbit=False)
    return finish(img)


def title_card(title, subtitle='', icon='cube', tone='blue'):
    """Шапка для простых экранов: покупки, поддержка, промокод, ожидание чека, подтверждение."""
    W, H = 1280, 460
    tones = {
        'blue': ((27, 42, 138), (60, 28, 130), (18, 24, 76), (70, 26, 140)),
        'green': ((18, 90, 90), (30, 60, 130), (12, 50, 60), (36, 40, 120)),
        'red': ((110, 30, 60), (70, 28, 120), (60, 18, 40), (50, 20, 90)),
    }
    img = canvas(W, H, *tones[tone])
    glow(img, [(1050, 230, 230, A2 if tone != 'green' else (40, 200, 150), 130), (150, 60, 200, A1, 100)])
    arcs(img, [(1240, 640, 420, A2, 100, 3), (1240, 640, 320, A1, 110, 2), (-80, -100, 280, (111, 139, 255), 70, 2)])
    glass(img, (60, 60, 1220, 400), 44)
    if icon == 'cube':
        cube(img, 1010, 215, 80)
    else:
        layer, d = overlay(img)
        cx, cy, r = 1010, 230, 105
        col = {'check': GREEN, 'clock': ORANGE, 'cross': RED, 'tag': (255, 220, 120), 'chat': (150, 190, 255), 'list': (190, 200, 255)}[icon]
        d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S], fill=col + (50,), outline=col + (200,), width=4 * S)
        w = 12 * S
        if icon == 'check':
            d.line([((cx - 45) * S, cy * S), ((cx - 12) * S, (cy + 35) * S), ((cx + 50) * S, (cy - 35) * S)], fill=col + (255,), width=w, joint='curve')
        elif icon == 'cross':
            d.line([((cx - 38) * S, (cy - 38) * S), ((cx + 38) * S, (cy + 38) * S)], fill=col + (255,), width=w)
            d.line([((cx + 38) * S, (cy - 38) * S), ((cx - 38) * S, (cy + 38) * S)], fill=col + (255,), width=w)
        elif icon == 'clock':
            d.line([(cx * S, (cy - 55) * S), (cx * S, cy * S), ((cx + 40) * S, (cy + 25) * S)], fill=col + (255,), width=w, joint='curve')
        elif icon == 'tag':
            d.polygon([((cx - 55) * S, (cy - 15) * S), ((cx - 15) * S, (cy - 55) * S), ((cx + 55) * S, (cy - 55) * S),
                       ((cx + 55) * S, (cy + 15) * S), ((cx + 15) * S, (cy + 55) * S)], outline=col + (255,), width=10 * S)
            d.ellipse([(cx + 18) * S, (cy - 38) * S, (cx + 38) * S, (cy - 18) * S], fill=col + (255,))
        elif icon == 'chat':
            d.rounded_rectangle([(cx - 60) * S, (cy - 45) * S, (cx + 60) * S, (cy + 30) * S], 24 * S, outline=col + (255,), width=10 * S)
            d.polygon([((cx - 30) * S, (cy + 25) * S), ((cx - 40) * S, (cy + 60) * S), ((cx) * S, (cy + 28) * S)], fill=col + (255,))
        elif icon == 'list':
            for i in range(3):
                y = cy - 40 + i * 40
                d.ellipse([(cx - 55) * S, (y - 8) * S, (cx - 39) * S, (y + 8) * S], fill=col + (255,))
                d.line([((cx - 22) * S, y * S), ((cx + 55) * S, y * S)], fill=col + (255,), width=10 * S)
        img.alpha_composite(layer)
    text(img, (120, 215 if subtitle else 250), title, fit(title, 76, 800, 740), 800, anchor='ls')
    if subtitle:
        text(img, (124, 285), subtitle, fit(subtitle, 34, 500, 740), 500, SOFT, anchor='ls')
    return finish(img)


if __name__ == '__main__':
    # предпросмотр: python cards.py <папка>
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else '.'
    samples = {
        'banner': banner('on'),
        'plan': plan_card('Неделя', '7 дней · отсчёт с активации', 270, 300, '−10% по приглашению'),
        'plan_gift': plan_card('Месяц', '30 дней · в подарок', 550, None, 'Подарок другу'),
        'status': status_card('4 д 6 ч', 'Неделя', 'до 28.10.2026 02:40 МСК', 0.6, 'on'),
        'invite': invite_card(),
        'gift': gift_card('@danil', 'Месяц', 'SC-7KQX-M2RD-H9PA', 'С днём рождения!'),
        'title_ok': title_card('Оплата подтверждена', 'ключ ниже', 'check', 'green'),
        'title_wait': title_card('Ждём чек', 'пришлите скриншот перевода', 'clock'),
    }
    for name, png in samples.items():
        with open(os.path.join(out, name + '.png'), 'wb') as f:
            f.write(png)
    print('ok')
