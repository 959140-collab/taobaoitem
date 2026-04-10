import tkinter as tk
from tkinter import messagebox, scrolledtext
import re
import os
import time
import queue
import threading
from io import BytesIO
from PIL import Image
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = lambda x: None
import platform
from pathlib import Path

try:
    import pytesseract
except ImportError:
    pytesseract = None

# ─── 历史记录文件 ──────────────────────────────────────────────
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'urls_history.txt')

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    return ''

def save_history(content):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        f.write(content)

# ─── 运行日志 ──────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'download.log')

class AppLogger:
    """追加写入 download.log，线程安全"""
    _lock = threading.Lock() if False else __import__('threading').Lock()

    def _write(self, level, msg):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] [{level}] {msg}"
        with self._lock:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    def start(self):
        self._write('START', '程序启动')

    def exit(self):
        self._write('EXIT ', '程序退出')

    def begin(self, url):
        self._write('BEGIN', f'开始下载: {url}')

    def success(self, title, save_path):
        self._write('OK   ', f'下载成功: 【{title}】 -> {save_path}')

    def failure(self, url, error):
        self._write('FAIL ', f'下载失败: {url} | 错误: {error}')

app_logger = AppLogger()

# ─── 工具函数 ──────────────────────────────────────────────────
def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\r\n\t]', '', name).strip()

def play_sound(success=True):
    """播放简短提示音：成功=轻柔短音，失败=低沉报警音"""
    sys_name = platform.system()
    try:
        if sys_name == 'Darwin':
            if success:
                os.system('afplay /System/Library/Sounds/Glass.aiff &')
            else:
                os.system('afplay /System/Library/Sounds/Basso.aiff &')
        elif sys_name == 'Windows':
            import winsound
            winsound.Beep(880 if success else 400, 200)
        else:
            print('\a', end='', flush=True)
    except Exception:
        pass

def system_beep():
    sys_name = platform.system()
    try:
        if sys_name == 'Windows':
            import winsound
            winsound.Beep(1000, 500)
        elif sys_name == 'Darwin':
            os.system('afplay /System/Library/Sounds/Glass.aiff &')
        else:
            print('\a', end='', flush=True)
    except Exception:
        pass

# ─── 爬虫核心 ─────────────────────────────────────────────────
class TaobaoScraper:
    def __init__(self, urls, log_fn):
        self.urls = urls
        self.log = log_fn
        self.logger = app_logger
        downloads_dir = os.path.join(str(Path.home()), 'Downloads', 'Taobao_Data')
        os.makedirs(downloads_dir, exist_ok=True)
        self.base_dir = downloads_dir

    def run(self):
        with sync_playwright() as p:
            user_data_dir = os.path.join(os.getcwd(), 'chrome_user_data')
            browser = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                viewport={'width': 1280, 'height': 800},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )
            page = browser.new_page()
            stealth_sync(page)

            total = len(self.urls)
            failed = False
            for idx, url in enumerate(self.urls, 1):
                if not url.strip():
                    continue
                self.log(f"\n[{idx}/{total}] 正在处理: {url}")
                self.logger.begin(url)
                try:
                    self.process_item(page, url.strip())
                except Exception as e:
                    self.log(f"\n❌ 下载失败，程序终止。错误: {e}")
                    self.logger.failure(url, e)
                    play_sound(success=False)
                    failed = True
                    break

            browser.close()
            if not failed:
                self.log(f"\n✅ 全部 {total} 件商品下载完成！文件保存在:\n{self.base_dir}")

    def process_item(self, page, url):
        try:
            # 网络拦截：捕获 .mp4 响应 URL
            captured_videos = []
            def _capture_video(resp):
                u = resp.url
                if '.mp4' in u and u.startswith('http'):
                    captured_videos.append(u)
            page.on('response', _capture_video)

            page.goto(url, timeout=60000)
            self.check_verification(page)
            self.scroll_page(page)

            # 滚动后稍等让视频请求完成
            import time as _t; _t.sleep(2)
            page.remove_listener('response', _capture_video)

            title = page.title().split('-')[0].strip()
            item_id = "unknown"
            match = re.search(r'id=(\d+)', url)
            if match:
                item_id = match.group(1)

            safe_title = sanitize_filename(title)
            if len(safe_title) > 35:
                safe_title = safe_title[:35]

            folder_name = f"{item_id}_{safe_title}"
            item_dir = os.path.join(self.base_dir, folder_name)
            img_dir = os.path.join(item_dir, "images")
            os.makedirs(img_dir, exist_ok=True)

            # 主图
            main_imgs = page.evaluate("""() => {
                let imgs = Array.from(document.querySelectorAll('.tb-gallery img, .mainPic--pic--1lJc90o, #J_ImgBooth, .gallery--img--1X_QkP3, div[class*="mainPic"] img, div[class*="gallery"] img'));
                return imgs.map(img => img.src).filter(src => src && !src.includes('lazy') && !src.includes('10x10.jpg'));
            }""")
            main_imgs = list(set([self.format_url(src) for src in main_imgs]))

            # 主图视频：网络拦截 + JS 双策略
            video_urls_set = set(captured_videos)
            js_videos = page.evaluate("""() => {
                let urls = new Set();
                document.querySelectorAll('video').forEach(v => {
                    if (v.src && v.src.startsWith('http')) urls.add(v.src);
                    v.querySelectorAll('source').forEach(s => {
                        if (s.src && s.src.startsWith('http')) urls.add(s.src);
                    });
                });
                document.querySelectorAll('script').forEach(sc => {
                    let text = sc.textContent || '';
                    let matches = text.match(/https?:\/\/[^"'\s,)]+\.mp4[^"'\s,)]*/g);
                    if (matches) matches.forEach(u => urls.add(u));
                });
                return Array.from(urls);
            }""")
            video_urls_set.update(js_videos)
            video_urls = list(video_urls_set)

            # 详情图
            desc_imgs = page.evaluate("""() => {
                let imgs = Array.from(document.querySelectorAll('#description img, #J_DivItemDesc img, div[class*="desc-"] img, div[class*="detail"] img'));
                return imgs.map(img => img.getAttribute('data-src') || img.src).filter(src => src && !src.includes('lazy'));
            }""")
            desc_imgs = list(set([self.format_url(src) for src in desc_imgs]))

            # 属性
            props = page.evaluate("""() => {
                let selectors = [
                    'ul[class*="attributes"] li',
                    'ul[class*="parameter"] li',
                    '#J_AttrUL li',
                    'table[class*="pkg-table"] tr',
                    'div[class*="Attrs--item"]',
                    'div[class*="BasicContent--item"]',
                    'div[class^="ItemProperty--"]',
                    'div[class*="generalParamsInfoItem--"]',
                    'div[class*="emphasisParamsInfoItem--"]'
                ];
                let items = Array.from(document.querySelectorAll(selectors.join(', ')));
                let results = items.map(el => { var s = el.innerText.trim(); return s.split(String.fromCharCode(10)).join('：'); }).filter(t => t.length > 2);
                if (results.length === 0) {
                    let targetPool = [];
                    for (let el of document.querySelectorAll('div')) {
                        if (el.innerText && el.innerText.includes('品牌') && el.innerText.includes('材质') && el.children.length > 3 && el.innerText.length < 2000) {
                            targetPool.push(el);
                        }
                    }
                    if (targetPool.length > 0) {
                        results = Array.from(targetPool[targetPool.length-1].children)
                            .map(el => { var s = el.innerText.trim(); return s.split(String.fromCharCode(10)).join('：'); })
                            .filter(t => t.length > 2);
                    }
                }
                return results;
            }""")

            # OCR 兜底
            if not props and pytesseract:
                self.log("  --> DOM 未找到属性，尝试 OCR 识别...")
                ocr_img_path = os.path.join(item_dir, "temp_ocr.png")
                param_box = page.locator('div:has-text("参数信息")').locator('..').first
                if param_box.count() == 0:
                    param_box = page.locator('.paramsInfoArea').first
                if param_box.count() > 0:
                    try:
                        param_box.screenshot(path=ocr_img_path)
                        text_data = pytesseract.image_to_string(Image.open(ocr_img_path), lang="chi_sim")
                        lines = [l.strip() for l in text_data.split('\n') if len(l.strip()) > 1]
                        for idx2 in range(0, len(lines)-1, 2):
                            if len(lines[idx2]) < 10 and len(lines[idx2+1]) < 30:
                                props.append(f"{lines[idx2]}：{lines[idx2+1]}")
                            else:
                                props.append(lines[idx2])
                                props.append(lines[idx2+1])
                    except Exception as e:
                        self.log(f"  --> OCR 失败: {e}")
                    finally:
                        if os.path.exists(ocr_img_path):
                            os.remove(ocr_img_path)

            self.log(f"  --> 主图 {len(main_imgs)} 张 | 详情图 {len(desc_imgs)} 张 | 视频 {len(video_urls)} 个 | 属性 {len(props)} 条")

            self.log(f"  [主图] 开始下载，共 {len(main_imgs)} 张...")
            for i, img_url in enumerate(main_imgs):
                self.log(f"    主图 {i+1}/{len(main_imgs)}: {img_url[:60]}...")
                self.download_image(img_url, os.path.join(img_dir, f"main_{i+1}.jpg"), page)

            self.log(f"  [详情图] 开始下载，共 {len(desc_imgs)} 张...")
            for i, img_url in enumerate(desc_imgs):
                self.log(f"    详情图 {i+1}/{len(desc_imgs)}: {img_url[:60]}...")
                self.download_image(img_url, os.path.join(img_dir, f"desc_{i+1}.jpg"), page)

            if video_urls:
                self.log(f"  [视频] 开始下载，共 {len(video_urls)} 个...")
                for i, v_url in enumerate(video_urls):
                    self.log(f"    视频 {i+1}/{len(video_urls)}: {v_url[:60]}...")
                    self.download_video(v_url, os.path.join(item_dir, f"video_{i+1}.mp4"), page)
            else:
                self.log("  [视频] 未检测到视频")

            self.log(f"  [属性] 写入 info.txt，共 {len(props)} 条...")
            with open(os.path.join(item_dir, "info.txt"), "w", encoding="utf-8") as f:
                f.write(f"原始URL: {url}\n")
                f.write(f"抓取时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("=== 核心属性参数 ===\n")
                written = 0
                for prop in props:
                    if not any(k in prop for k in ['价格', '促销', '销量', '评价', '详情']):
                        f.write(f"{prop}\n")
                        written += 1
                        self.log(f"    属性 {written}: {prop[:50]}")

            self.log(f"  ✅ 【{safe_title}】下载完成")
            self.log(f"     保存路径: {item_dir}")
            self.log(f"OPEN_DIR::{item_dir}")
            self.logger.success(safe_title, item_dir)
            play_sound(success=True)

        except Exception as e:
            self.log(f"  ❌ 处理失败: {e}")
            raise

    def check_verification(self, page):
        while True:
            title = page.title()
            is_blocked = any(k in title for k in ["验证码", "滑块", "安全验证", "登录", "login", "Login"])
            if not is_blocked:
                is_blocked = page.locator(".nc-container, #baxia-dialog-content, #login, #J_LoginBox, .baxia-dialog").count() > 0
            if is_blocked:
                system_beep()
                self.show_top_dialog(
                    "安全拦截或验证检测",
                    "检测到验证码、滑块或需要手动登录。\n\n请在浏览器中【手动处理】后，点击确定继续。"
                )
                time.sleep(2)
            else:
                break

    def show_top_dialog(self, title, msg):
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showwarning(title, msg, parent=root)
        root.destroy()

    def scroll_page(self, page):
        page.mouse.wheel(0, 300)
        time.sleep(1)
        for _ in range(25):
            page.mouse.wheel(0, 600)
            time.sleep(0.4)

    def format_url(self, url):
        url = re.sub(r'_\.\d+x\d+[a-zA-Z]*\.jpg$', '', url)
        if url.startswith('//'):
            return 'https:' + url
        elif url.startswith('/'):
            return 'https://' + url
        return url

    def download_video(self, url, save_path, page):
        """下载视频文件（分块流式写入，支持大文件）"""
        try:
            import urllib.request
            headers = {
                'User-Agent': page.evaluate("navigator.userAgent"),
                'Referer': page.url
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(save_path, 'wb') as f:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 每次读 1MB
                        if not chunk:
                            break
                        f.write(chunk)
            size_mb = os.path.getsize(save_path) / 1024 / 1024
            self.log(f"    [✓] 视频保存成功 ({size_mb:.1f} MB): {save_path}")
        except Exception as e:
            self.log(f"    [!] 视频下载失败: {e}")

    def download_image(self, url, save_path, page):
        try:
            headers = {
                'User-Agent': page.evaluate("navigator.userAgent"),
                'Referer': page.url
            }
            resp = page.context.request.get(url, headers=headers, timeout=15000)
            if resp.ok:
                body = resp.body()
                if len(body) < 10 * 1024:
                    self.log("  --> 图片小于 10KB，视为无效图片并忽略")
                    return
                img = Image.open(BytesIO(body))
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                if not save_path.lower().endswith('.jpg'):
                    save_path = save_path.rsplit('.', 1)[0] + '.jpg'
                img.save(save_path, "JPEG", quality=95)
        except Exception as e:
            self.log(f"  --> 图片下载失败: {e}")


# ─── 单窗口双页面主程序 ──────────────────────────────────────
def run_app():
    root = tk.Tk()
    root.title("淘宝/天猫商品数据抓取助手")
    root.geometry("700x540")
    root.eval('tk::PlaceWindow . center')

    log_queue = queue.Queue()

    # ── 页面容器 ──────────────────────────────────────
    input_frame   = tk.Frame(root)
    progress_frame = tk.Frame(root)

    def show_input_page():
        progress_frame.pack_forget()
        input_frame.pack(fill='both', expand=True)

    def show_progress_page():
        input_frame.pack_forget()
        progress_frame.pack(fill='both', expand=True)

    # ════════════════════════════════════════════════
    # 输入页
    # ════════════════════════════════════════════════
    input_top = tk.Frame(input_frame)
    input_top.pack(fill='x', padx=10, pady=6)
    tk.Label(input_top, text="商品 URL 输入",
             font=("Arial", 13, "bold")).pack(side='left')
    tk.Button(input_top, text="查看进度 ▶", command=show_progress_page,
              font=("Arial", 12), padx=10, pady=3).pack(side='right')

    tk.Label(input_frame, text="请粘贴淘宝或天猫的商品 URL（每行一条）:",
             font=("Arial", 14)).pack(pady=6)

    text_area = tk.Text(input_frame, width=82, height=20)
    text_area.pack(padx=12, pady=6)

    history = load_history()
    if history:
        text_area.insert('1.0', history)

    def on_submit():
        content = text_area.get("1.0", "end-1c").strip()
        if not content:
            return
        save_history(content)
        urls = [l.strip() for l in content.split('\n') if l.strip()]
        # 清空日志框，准备新一轮
        log_box.configure(state='normal')
        log_box.delete('1.0', 'end')
        log_box.configure(state='disabled')
        status_var.set("正在初始化...")
        show_progress_page()
        # 启动爬虫线程
        t = threading.Thread(target=lambda: run_scraper(urls), daemon=True)
        t.start()

    def on_clear():
        text_area.delete('1.0', 'end')
        save_history('')

    def on_paste():
        try:
            clipboard = root.clipboard_get()
        except Exception:
            return
        # 逐行过滤：只保留以 http:// 或 https:// 开头的行
        valid_lines = [l.strip() for l in clipboard.splitlines()
                       if l.strip().startswith(('http://', 'https://'))]
        if not valid_lines:
            return
        # 追加到输入框（末尾换行后插入）
        current = text_area.get('1.0', 'end-1c')
        if current and not current.endswith('\n'):
            text_area.insert('end', '\n')
        text_area.insert('end', '\n'.join(valid_lines))

    btn_row = tk.Frame(input_frame)
    btn_row.pack(pady=10)
    tk.Button(btn_row, text="粘贴", command=on_paste,
              font=("Arial", 13), padx=14, pady=4).pack(side='left', padx=10)
    tk.Button(btn_row, text="清空", command=on_clear,
              font=("Arial", 13), padx=14, pady=4).pack(side='left', padx=10)
    tk.Button(btn_row, text="开始抓取 ▶", command=on_submit,
              font=("Arial", 13, "bold"), padx=14, pady=4).pack(side='left', padx=10)

    text_area.focus_set()

    # ════════════════════════════════════════════════
    # 进度页
    # ════════════════════════════════════════════════
    top_bar = tk.Frame(progress_frame)
    top_bar.pack(fill='x', padx=10, pady=6)
    tk.Button(top_bar, text="◀ 返回输入", command=show_input_page,
              font=("Arial", 12), padx=10, pady=3).pack(side='left')
    tk.Label(top_bar, text="下载进度信息（可直接复制）",
             font=("Arial", 13, "bold")).pack(side='left', padx=16)

    log_box = scrolledtext.ScrolledText(
        progress_frame, width=88, height=26, state='disabled',
        font=("Courier", 11), bg="#1e1e1e", fg="#d4d4d4",
        insertbackground='white'
    )
    log_box.pack(padx=10, pady=4, fill='both', expand=True)

    status_var = tk.StringVar(value="")
    tk.Label(progress_frame, textvariable=status_var,
             font=("Arial", 11), fg="#555").pack(pady=5)

    # ── 日志轮询 ──────────────────────────────────────
    # 链接样式标签（只创建一次）
    _link_tag_counter = [0]

    def open_directory(path):
        import subprocess
        sys_name = platform.system()
        try:
            if sys_name == 'Darwin':
                subprocess.Popen(['open', path])
            elif sys_name == 'Windows':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            pass

    def poll_queue():
        try:
            while True:
                msg = log_queue.get_nowait()
                log_box.configure(state='normal')
                if msg.startswith('OPEN_DIR::'):
                    path = msg[len('OPEN_DIR::'):]
                    tag = f"link_{_link_tag_counter[0]}"
                    _link_tag_counter[0] += 1
                    link_text = f"     📂 点击打开目录: {path}"
                    log_box.insert('end', link_text + '\n', tag)
                    log_box.tag_config(tag, foreground='#4fc3f7', underline=True)
                    log_box.tag_bind(tag, '<Button-1>', lambda e, p=path: open_directory(p))
                    log_box.tag_bind(tag, '<Enter>', lambda e: log_box.config(cursor='hand2'))
                    log_box.tag_bind(tag, '<Leave>', lambda e: log_box.config(cursor=''))
                else:
                    log_box.insert('end', msg + '\n')
                log_box.see('end')
                log_box.configure(state='disabled')
                clean = msg.strip()
                if clean and not clean.startswith('OPEN_DIR::'):
                    status_var.set(clean[:90])
        except queue.Empty:
            pass
        root.after(150, poll_queue)

    def run_scraper(urls):
        scraper = TaobaoScraper(urls, lambda msg: log_queue.put(msg))
        scraper.run()

    root.after(150, poll_queue)
    show_input_page()
    root.mainloop()


# ─── 入口 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    app_logger.start()
    try:
        run_app()
    finally:
        app_logger.exit()
