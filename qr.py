"""
スマホで開くアドレスの QR コードを SVG で作る（標準ライブラリだけ）。
バイトモード・誤り訂正 M・型番 1〜10（約 200 文字まで）。URL を載せるだけなのでこれで足りる。
  import qr; svg = qr.svg("http://192.168.0.2:8200/")
"""

# 型番 1〜10、誤り訂正 M のブロックごとの誤り訂正語数とブロック数
EC_PER_BLOCK = [10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
BLOCKS = [1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
FORMAT_M = 0  # 形式情報での M の 2 ビット


def gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = gf_mul(root, 0x02)
    return result


def rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= gf_mul(coef, factor)
    return result


def raw_modules(ver):
    """型番の中で、データと誤り訂正に使えるモジュールの数"""
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def data_codewords(ver):
    return raw_modules(ver) // 8 - EC_PER_BLOCK[ver - 1] * BLOCKS[ver - 1]


def encode_data(data, ver):
    """モード・文字数・データ・終端・埋め草までのデータ語"""
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))
    put(0b0100, 4)
    put(len(data), 8 if ver < 10 else 16)
    for b in data:
        put(b, 8)
    cap = data_codewords(ver) * 8
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(out) < cap // 8:
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    return out


def add_ec(data, ver):
    """ブロックに分けて誤り訂正語を付け、交互に並べる"""
    nblocks, ec = BLOCKS[ver - 1], EC_PER_BLOCK[ver - 1]
    total = raw_modules(ver) // 8
    short = nblocks - total % nblocks
    short_len = total // nblocks - ec  # 短いブロックのデータ語数（長いブロックは +1）
    div = rs_divisor(ec)
    blocks, k = [], 0
    for i in range(nblocks):
        n = short_len + (0 if i < short else 1)
        d = data[k:k + n]
        k += n
        blocks.append((d, rs_remainder(d, div)))
    out = []
    for i in range(short_len + 1):
        out += [d[i] for d, _ in blocks if i < len(d)]
    for i in range(ec):
        out += [e[i] for _, e in blocks]
    return out


def matrix(data, ver):
    size = ver * 4 + 17
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def setf(x, y, on):
        dark[y][x] = on
        fixed[y][x] = True

    for i in range(size):  # タイミングパターン
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # 位置検出パターンと分離帯
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    if ver >= 2:  # 位置合わせパターン
        n = ver // 7 + 2
        step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        pos = [6] + sorted(size - 7 - i * step for i in range(n - 1))
        for i, cx in enumerate(pos):
            for j, cy in enumerate(pos):
                if (i, j) in ((0, 0), (0, n - 1), (n - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    draw_format(setf, size, 0)  # 場所だけ先に取る（あとでマスクに合わせて書き直す）
    if ver >= 7:  # 型番情報
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = ver << 12 | rem
        for i in range(18):
            on = (bits >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            setf(a, b, on)
            setf(b, a, on)

    i, right = 0, size - 1  # データを右下から2列ずつジグザグに置く
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fixed[y][x] and i < len(data) * 8:
                    dark[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2
    return dark, fixed


def draw_format(setf, size, mask):
    data = FORMAT_M << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412

    def bit(i):
        return (bits >> i) & 1 == 1
    for i in range(6):
        setf(8, i, bit(i))
    setf(8, 7, bit(6))
    setf(8, 8, bit(7))
    setf(7, 8, bit(8))
    for i in range(9, 15):
        setf(14 - i, 8, bit(i))
    for i in range(8):
        setf(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        setf(8, size - 15 + i, bit(i))
    setf(8, size - 8, True)


MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def apply_mask(dark, fixed, mask):
    f = MASKS[mask]
    return [[d != (not fx and f(x, y)) for x, (d, fx) in enumerate(zip(row, frow))]
            for y, (row, frow) in enumerate(zip(dark, fixed))]


def penalty(m):
    """読み取りにくさの点数（小さいほどよい）。マスクを選ぶのに使う"""
    size, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    finder = ([True, False, True, True, True, False, True, False, False, False, False],
              [False, False, False, False, True, False, True, True, True, False, True])
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        for i in range(size - 10):
            if line[i:i + 11] in finder:
                score += 40
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    total = size * size
    darks = sum(map(sum, m))
    score += ((abs(darks * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def modules(text):
    """QR コードのモジュール（True が黒）。収まらなければ ValueError"""
    data = text.encode("utf-8")
    for ver in range(1, 11):
        if 4 + (8 if ver < 10 else 16) + len(data) * 8 <= data_codewords(ver) * 8:
            break
    else:
        raise ValueError("QR コードに入りきらない長さ")
    dark, fixed = matrix(add_ec(encode_data(data, ver), ver), ver)
    best = None
    for mask in range(8):
        m = apply_mask(dark, fixed, mask)
        draw_format(lambda x, y, on: m[y].__setitem__(x, on), len(m), mask)
        p = penalty(m)
        if best is None or p < best[0]:
            best = (p, m)
    return best[1]


def svg(text, quiet=4):
    m = modules(text)
    n = len(m) + quiet * 2
    path = "".join(f"M{x + quiet} {y + quiet}h1v1h-1z"
                   for y, row in enumerate(m) for x, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
