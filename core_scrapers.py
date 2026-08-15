import time
import json
import re
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from proxy_manager import get_best_proxy_for_target
from m3u_generator import create_slug

def create_driver(proxy_ip=None, protocol="http"):
    chrome_options = Options()
    
    # FIX: Tối ưu hoá cho môi trường GitHub Actions chạy qua Proxy chậm.
    # Chiến lược tải trang "eager" giúp báo thành công ngay khi tải xong DOM (bỏ qua ảnh, quảng cáo, JS bên thứ 3)
    chrome_options.page_load_strategy = 'eager' 
    
    chrome_options.add_argument("--headless=new") 
    
    # FIX: Cấu hình bắt buộc để tránh Crash/Treo Chrome trên server Linux/Windows của GitHub Actions
    chrome_options.add_argument("--no-sandbox") 
    chrome_options.add_argument("--disable-dev-shm-usage") 
    chrome_options.add_argument("--disable-gpu")
    
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--log-level=3") 
    chrome_options.add_argument("--autoplay-policy=no-user-gesture-required")
    
    # FIX: Vượt tường lửa WAF của VTV/Kong API (Block dấu hiệu Webdriver & Headless khi dùng IP Datacenter)
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    chrome_options.add_argument("--ignore-certificate-errors")
    
    chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    if proxy_ip:
        chrome_options.add_argument(f'--proxy-server={protocol}://{proxy_ip}')
            
    driver = webdriver.Chrome(options=chrome_options)
    
    # WORKAROUND: Tiêm Javascript xoá cờ navigator.webdriver ngay khi khởi tạo trang trắng
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    })
    return driver

def reboot_driver(driver, proxy_ip=None, protocol="http"):
    if driver:
        try:
            driver.execute_cdp_cmd('Network.clearBrowserCache', {})
            driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
            driver.quit()
        except: pass
    return create_driver(proxy_ip, protocol)

def catch_m3u8_vtvgo(driver, url, max_wait=60):
    try:
        driver.set_page_load_timeout(max_wait)
        driver.get_log('performance') 
        driver.get(url)
        
        # WORKAROUND: Cắt lỗ Fail-Fast. Nếu Proxy sập, Chrome sẽ trả về trang hiển thị lỗi thay vì treo.
        page_src = driver.page_source
        if "ERR_CONNECTION" in page_src or "ERR_PROXY" in page_src or "ERR_TIMED_OUT" in page_src:
            return None, "Proxy chết giữa chừng (Báo động mạng)"

        # FIX: Loại bỏ sleep tĩnh, chuyển logic click vào vòng lặp để đối phó với mạng chậm trên GitHub Actions.
        for i in range(max_wait):  
            # WORKAROUND: Bám đuổi click Popup Điều khoản và Play Video liên tục mỗi giây
            try:
                driver.execute_script("""
                    var btns = document.getElementsByTagName('button');
                    for (var i=0; i<btns.length; i++) {
                        if(btns[i].innerText.includes('Đồng ý') || btns[i].innerText.includes('tiếp tục')) btns[i].click();
                    }
                    var vids = document.getElementsByTagName('video');
                    if (vids.length > 0 && vids[0].paused) vids[0].play();
                """)
            except: pass

            logs = driver.get_log('performance')
            for entry in logs:
                try:
                    log_data = json.loads(entry['message'])['message']
                    if 'Network.requestWillBeSent' in log_data['method']:
                        req_url = log_data['params']['request']['url']
                        vtv_keywords = ['vtv', 'cdn', 'stream', 'live', 'media', 'truyenhinhso', 'mediatech', 'playlist', 'manifest']
                        if '.m3u8' in req_url and any(kw in req_url.lower() for kw in vtv_keywords):
                            return req_url, "OK"
                except: continue
            time.sleep(1)
        return None, f"Timeout {max_wait}s"
    except Exception as e:
        return None, f"Lỗi System: {str(e)[:30]}"

def catch_m3u8_tv360(driver, url, max_wait=60):
    try:
        driver.set_page_load_timeout(max_wait)
        driver.get_log('performance') 
        driver.get(url)
        
        # WORKAROUND: Báo động mạng Fail-Fast cho TV360
        page_src = driver.page_source
        if "ERR_CONNECTION" in page_src or "ERR_PROXY" in page_src or "ERR_TIMED_OUT" in page_src:
            return None, "Proxy chết giữa chừng (Báo động mạng)"
            
        time.sleep(3) 
        is_premium = driver.execute_script("return document.body.innerText.includes('Nội dung có phí') || document.body.innerText.includes('Vui lòng đăng ký gói');")
        if is_premium: return None, "PREMIUM"
        try: driver.execute_script("var v=document.querySelector('video'); if(v) v.play();")
        except: pass

        for i in range(max_wait): 
            logs = driver.get_log('performance')
            for entry in logs:
                try:
                    log_data = json.loads(entry['message'])['message']
                    if 'Network.requestWillBeSent' in log_data['method']:
                        req_url = log_data['params']['request']['url']
                        if '.m3u8' in req_url and 'uid=' in req_url:
                            return req_url, "OK"
                except: continue
            time.sleep(1)
        return None, f"Timeout {max_wait}s"
    except Exception as e:
        return None, f"Lỗi System: {str(e)[:30]}"

def scan_channels_with_rotation(driver, channels, platform, old_links_dict, exclude_set, current_proxy_ip, current_protocol, proxy_stats, vn_proxies, use_auto_proxy, logger):
    i = 0
    consecutive_fails = 0

    while i < len(channels):
        ch = channels[i]

        if ch.get('skip'):
            i += 1
            continue

        f_link = None
        s_msg = ""
        has_old_link = ch['name'] in old_links_dict and old_links_dict[ch['name']]['url']
        
        if not ch.get('url'):
            if has_old_link:
                ch['m3u8_link'] = old_links_dict[ch['name']]['url']
                ch['source'] = 'fallback_only'
                logger(f"[{platform.upper()}/Scanner] - [FALLBACK] - Kênh {ch['name']}: Áp dụng Link Fallback thành công.")
            else:
                ch['error_msg'] = "Không có URL để quét"
                logger(f"[{platform.upper()}/Scanner] - [FAILED] - Kênh {ch['name']}: Không có URL web, không có link cũ.")
            i += 1
            continue

        timeouts = [60, 120, 240] if has_old_link else [60]

        for t in timeouts:
            logger(f"[{platform.upper()}/Scanner] - [PROCESS] - Cào kênh {ch['name']} (Chờ tối đa {t}s)...")
            
            if platform == 'vtv':
                f_link, s_msg = catch_m3u8_vtvgo(driver, ch['url'], max_wait=t)
            else:
                f_link, s_msg = catch_m3u8_tv360(driver, ch['url'], max_wait=t)

            if s_msg == "PREMIUM":
                ch['skip'] = True
                break 
            if f_link:
                break 

            if has_old_link and t != timeouts[-1]:
                logger(f"[{platform.upper()}/Scanner] - [RETRY] - Lỗi: {s_msg}. Khởi động lại trình duyệt...")
                driver = reboot_driver(driver, current_proxy_ip, current_protocol)

        if f_link:
            ch['m3u8_link'] = f_link
            consecutive_fails = 0
            if current_proxy_ip:
                proxy_key = f"{current_protocol}://{current_proxy_ip}"
                proxy_stats[proxy_key] = proxy_stats.get(proxy_key, 0) + 1 
            logger(f"[{platform.upper()}/Scanner] - [SUCCESS] - Lấy Link Thành Công")
            i += 1
        elif ch.get('skip'):
            logger(f"[{platform.upper()}/Scanner] - [SKIP] - Kênh Thu Phí. Bỏ qua.")
            consecutive_fails = 0
            i += 1
        else:
            consecutive_fails += 1
            if has_old_link:
                ch['m3u8_link'] = old_links_dict[ch['name']]['url']
                ch['source'] = 'fallback_only'
                logger(f"[{platform.upper()}/Scanner] - [FALLBACK] - Thất bại ({s_msg}). Dùng Fallback lấy từ file cũ.")
            else:
                ch['error_msg'] = "Lỗi toàn tập"
                logger(f"[{platform.upper()}/Scanner] - [FAILED] - Thất bại hoàn toàn (Không có file cũ).")

            # BUSINESS RULE: Nếu 3 kênh liên tiếp thất bại, IP hiện tại có khả năng đã bị Server chặn ngầm.
            # Cần đổi Proxy mới và lùi lại (rewind) 3 bước để cào lại các kênh bị đánh dấu lỗi oan.
            if consecutive_fails >= 3:
                logger(f"[{platform.upper()}/Scanner] - [WARNING] - 3 kênh liên tiếp thất bại. Cần đổi IP!")
                if current_proxy_ip:
                    exclude_set.add(current_proxy_ip) 
                
                logger(f"[{platform.upper()}/Proxy] - [FETCH] - Đang tìm Proxy mới thay thế...")
                new_proxy_ip, new_protocol = get_best_proxy_for_target(vn_proxies, platform, exclude_set, logger)

                if new_proxy_ip:
                    logger(f"[{platform.upper()}/Proxy] - [SUCCESS] - Đổi IP: {new_proxy_ip}. Quay lui 3 bước...")
                    current_proxy_ip = new_proxy_ip
                    current_protocol = new_protocol
                    driver = reboot_driver(driver, current_proxy_ip, current_protocol)
                    consecutive_fails = 0

                    back_steps = 3
                    start_rewind = max(0, i - back_steps + 1)
                    for rewind_idx in range(start_rewind, i + 1):
                        channels[rewind_idx]['m3u8_link'] = None
                        channels[rewind_idx]['source'] = channels[rewind_idx]['original_source']
                        channels[rewind_idx]['error_msg'] = None
                    i = start_rewind 
                else:
                    logger(f"[{platform.upper()}/Proxy] - [EXHAUSTED] - Kho IP cạn kiệt. Tiếp tục cào bằng Fallback...")
                    consecutive_fails = 0 
                    i += 1
            else:
                i += 1
    return driver

def _intercept_vtv_api_and_link(driver, timeout_sec, logger):
    api_auth_headers = None
    captured_api_json = None
    temp_vtv_master_link = None
    vtv_keywords = ['vtv', 'cdn', 'stream', 'live', 'media', 'truyenhinhso', 'mediatech', 'playlist', 'manifest']
    
    for wait_sec in range(timeout_sec):
        try:
            driver.execute_script("""
                var btns = document.getElementsByTagName('button');
                for (var i=0; i<btns.length; i++) {
                    if(btns[i].innerText.includes('Đồng ý') || btns[i].innerText.includes('tiếp tục')) btns[i].click();
                }
                var vids = document.getElementsByTagName('video');
                if (vids.length > 0 && vids[0].paused) vids[0].play();
            """)
        except: pass

        logs = driver.get_log('performance')
        for entry in logs:
            try:
                log_msg = json.loads(entry['message'])['message']
                method = log_msg['method']
                
                if not api_auth_headers and method == 'Network.requestWillBeSent':
                    req_params = log_msg['params']['request']
                    req_url = req_params['url']
                    req_method = req_params.get('method', '').upper()
                    
                    if 'live-channel/api/v1/channels/byCatalog' in req_url and req_method != 'OPTIONS':
                        temp_headers = req_params.get('headers', {})
                        if any(k.lower() == 'authorization' for k in temp_headers.keys()):
                            api_auth_headers = temp_headers
                            logger(f"[VTV/Network] - [INTERCEPT] - Đã trộm Headers/Token API: {req_url.split('?')[0]}")
                            
                if not captured_api_json and method == 'Network.responseReceived':
                    resp_url = log_msg['params']['response']['url']
                    if 'live-channel/api/v1/channels/byCatalog' in resp_url:
                        req_id = log_msg['params']['requestId']
                        try:
                            body = driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': req_id})
                            captured_api_json = json.loads(body['body'])
                        except: pass

                if not temp_vtv_master_link and method == 'Network.requestWillBeSent':
                    req_url = log_msg['params']['request']['url']
                    if '.m3u8' in req_url and any(kw in req_url.lower() for kw in vtv_keywords):
                        temp_vtv_master_link = req_url
                        logger(f"[VTV/Network] - [INTERCEPT] - Bắt được Link Gốc M3U8: {req_url.split('?')[0]}")
                        
            except Exception: continue
            
        if api_auth_headers and temp_vtv_master_link:
            logger(f"[VTV/Network] - [READY] - Có đủ Token & Link M3U8 (sau {wait_sec+1}s).")
            break
        time.sleep(1)
    return api_auth_headers, captured_api_json, temp_vtv_master_link

def _fetch_vtv_channels_via_js(driver, api_auth_headers, logger):
    logger("[VTV/Fetch] - [REQUEST] - Gửi Fetch Request bằng JS để lấy FULL 500 Kênh...")
    clean_headers = {k: v for k, v in api_auth_headers.items() if not k.startswith(':')}
    js_fetch = f"""
    var callback = arguments[arguments.length - 1];
    fetch("https://web-api-vtvgo.vtvdigital.vn/live-channel/api/v1/channels/byCatalog?page=1&limit=500", {{
        method: "GET",
        headers: {json.dumps(clean_headers)}
    }})
    .then(res => res.json())
    .then(data => callback({{success: true, data: data}}))
    .catch(err => callback({{success: false, error: err.toString()}}));
    """
    try:
        driver.set_script_timeout(15) 
        res = driver.execute_async_script(js_fetch)
        if res and res.get('success'):
            logger("[VTV/Fetch] - [SUCCESS] - Fetch JS thủ công hoàn tất.")
            return res.get('data')
        else:
            logger(f"[VTV/Fetch] - [FAILED] - Fetch JS lỗi: {res.get('error')}")
    except Exception as req_err:
        logger(f"[VTV/Fetch] - [ERROR] - Ngoại lệ gọi Fetch JS: {req_err}")
    return None

def _parse_vtv_json_data(api_json_data, logger):
    logger("[VTV/Parser] - [START] - Phân tích dữ liệu JSON VTV...")
    vtv_channels = []
    groups = api_json_data['data'].get('channels', [])
    count_channels = 0
    for item in groups:
        if 'channels' in item: 
            gn_name = item.get('name', 'Khác')
            ch_list = item.get('channels', [])
        else: 
            gn_name = 'Khác'
            ch_list = [item]

        for c in ch_list:
            gn_lower = gn_name.lower()
            ch_name_lower = c.get('name', '').lower()
            
            # BUSINESS RULE: Chặn đứng các kênh VTVCab / ON Sports trả phí lọt thỏm trong nhóm "Trong nước"
            # Cập nhật: Chặn thêm nhóm "ONE VTV Sport" để ngăn các kênh SCTV cao cấp lọt lưới nội suy hỏng.
            if ch_name_lower.startswith('on ') or any(kw in gn_lower or kw in ch_name_lower for kw in ['vtvcab', 'one vtv']):
                continue
            
            if any(kw in gn_lower or kw in ch_name_lower for kw in ['vtv', 'sctv', 'địa phương', 'dia phuong', 'trong nước', 'thiết yếu']):
                # BUSINESS RULE: Mọi kênh VTV (ngoại trừ SCTV) đều bị ép thành 'vtvgo_dynamic'
                # Điều này ngăn chặn việc tự nội suy Link dễ dính lỗi nếu URL VTV thay đổi.
                src_type = 'vtvgo_static' if 'sctv' in gn_lower or 'sctv' in ch_name_lower else 'vtvgo_dynamic'
                slug = create_slug(c.get('name')) if not c.get('slug') else c.get('slug')
                vtv_channels.append({
                    'id': str(c.get('id')), 'name': c.get('name'), 'logo': c.get('logo', ''),
                    'group_name': gn_name if gn_name != 'Khác' else 'Địa phương', 
                    'source': src_type, 'original_source': src_type, 
                    'url': f"https://vtvgo.vn/channel/{slug}-1,{c.get('id')}.html",
                    'm3u8_link': None, 'error_msg': None, 'skip': False
                })
                count_channels += 1

    if vtv_channels:
        logger(f"[VTV/Parser] - [SUCCESS] - Phân tích được {count_channels} kênh VTV/SCTV/Địa Phương (Đã lọc sạch ON/VTVCab/ONE VTV).")
    return vtv_channels

def _parse_vtv_fallback_dom(page_source, logger):
    logger("[VTV/Parser] - [FALLBACK] - Thử Fallback Cổ Điển DOM __INITIAL_STATE__...")
    vtv_channels = []
    match = re.search(r'<script id="__INITIAL_STATE__" type="application/json">(.*?)</script>', page_source)
    if match:
        state_json = json.loads(match.group(1))
        groups = state_json.get('global', {}).get('dataList', {}).get('channel-by-catalog-all', {}).get('channels', [])
        for group in groups:
            gn_name = group.get('name', 'Khác')
            gn_lower = gn_name.lower()
            
            # BUSINESS RULE: Chặn đứng nhóm VTVCab và ONE VTV
            if 'vtvcab' in gn_lower or 'one vtv' in gn_lower: continue
            
            if any(kw in gn_lower for kw in ['vtv', 'sctv', 'địa phương', 'dia phuong', 'trong nước', 'thiết yếu']):
                # BUSINESS RULE: Ép kênh VTV phải cào thực tế, chỉ SCTV mới cho đoán link (static)
                src_type = 'vtvgo_static' if 'sctv' in gn_lower else 'vtvgo_dynamic'
                for c in group.get('channels', []):
                    ch_name_lower = c.get('name', '').lower()
                    
                    # BUSINESS RULE: Chặn đứng các kênh VTVCab / ON Sports / ONE VTV trả phí
                    if ch_name_lower.startswith('on ') or 'vtvcab' in ch_name_lower or 'one vtv' in ch_name_lower:
                        continue
                        
                    slug = create_slug(c.get('name')) if not c.get('slug') else c.get('slug')
                    vtv_channels.append({
                        'id': str(c.get('id')), 'name': c.get('name'), 'logo': c.get('logo', ''),
                        'group_name': gn_name, 'source': src_type, 'original_source': src_type, 
                        'url': f"https://vtvgo.vn/channel/{slug}-1,{c.get('id')}.html",
                        'm3u8_link': None, 'error_msg': None, 'skip': False
                    })
    return vtv_channels

def _get_active_proxy(platform, alive_cached, exclude_proxies, vn_proxies, use_auto_proxy, logger, current_ip=None, current_proto="http"):
    if not use_auto_proxy: return None, "http"
    if current_ip: return current_ip, current_proto
    
    if alive_cached.get(platform) and alive_cached[platform]["ip"] not in exclude_proxies:
        ip, proto = alive_cached[platform]["ip"], alive_cached[platform]["protocol"]
        logger(f"[{platform.upper()}/Proxy] - [CACHE_HIT] - Dùng IP Cache: {ip}")
        return ip, proto
        
    logger(f"[{platform.upper()}/Proxy] - [FETCH] - Lấy Proxy mới từ danh sách...")
    ip, proto = get_best_proxy_for_target(vn_proxies, platform, exclude_proxies, logger)
    return ip, proto

def _vtv_extract_dom_loop(driver, vtv_ip, vtv_proto, logger):
    vtv_channels = []
    vtv_master_link = None
    dom_success = False

    for t in [60, 120, 240]:
        logger(f"[VTV/DOM] - [START] - Truy cập VTV1 (Timeout: {t}s)")
        try:
            driver.set_page_load_timeout(t)
            driver.get_log('performance') 
            driver.get("https://vtvgo.vn/channel/vtv1-1,1.html")
            
            # WORKAROUND: Cắt lỗ nhanh nếu trình duyệt hiển thị màn hình báo lỗi Proxy từ Chrome
            page_src = driver.page_source
            if "ERR_CONNECTION" in page_src or "ERR_PROXY" in page_src or "ERR_TIMED_OUT" in page_src:
                logger("[VTV/DOM] - [FAILED] - 🚨 Proxy Dead: Trình duyệt báo lỗi mạng")
                raise Exception("Proxy Dead: Trình duyệt trả về trang báo lỗi mạng")

            api_auth_headers, captured_api_json, temp_vtv_master_link = _intercept_vtv_api_and_link(driver, t, logger)
                
            api_json_data = None
            if api_auth_headers:
                api_json_data = _fetch_vtv_channels_via_js(driver, api_auth_headers, logger)

            if not api_json_data and captured_api_json:
                logger("[VTV/DOM] - [FALLBACK] - ⚠️ Sử dụng JSON bị giới hạn bắt được từ trình duyệt.")
                api_json_data = captured_api_json

            if api_json_data and 'data' in api_json_data:
                vtv_channels = _parse_vtv_json_data(api_json_data, logger)
                if vtv_channels: dom_success = True
            else:
                vtv_channels = _parse_vtv_fallback_dom(driver.page_source, logger)
                if vtv_channels: dom_success = True
                    
            if temp_vtv_master_link:
                vtv_master_link = temp_vtv_master_link
                    
            # BUSINESS RULE: Nếu lấy được DOM thì phá vỡ vòng lặp retry ngay lập tức.
            if dom_success:
                if vtv_master_link:
                    for ch in vtv_channels:
                        if ch['name'].upper() == "VTV1":
                            ch['m3u8_link'] = vtv_master_link
                            ch['skip'] = True 
                            break
                    logger("[VTV/DOM] - [SUCCESS] - Lấy thành công cả DOM và Link Gốc VTV1.")
                else:
                    logger("[VTV/DOM] - [INFO] - Lấy được DOM nhưng thiếu Link Gốc VTV1. Hệ thống sẽ chuyển VTV1 sang quét ngầm...")
                break 

        except Exception as ex: 
            logger(f"[VTV/DOM] - [ERROR] - Ngoại lệ: {ex}")
        
        # Chỉ debug và khởi động lại nếu THẬT SỰ không lấy được DOM
        if not dom_success:
            try:
                logger(f"[VTV/Debug] - [INFO] - Tiêu đề trang hiện tại: {driver.title}")
                page_src = driver.page_source
                clean_html = re.sub(r'\s+', ' ', page_src[:1000])
                logger(f"[VTV/Debug] - [INFO] - 1000 ký tự HTML:\n{clean_html}")
                screenshot_path = os.path.join(os.getcwd(), f"debug_vtv_proxy_{int(time.time())}.png")
                driver.save_screenshot(screenshot_path)
                logger(f"[VTV/Debug] - [INFO] - 📸 Đã lưu ảnh: {screenshot_path}")
            except Exception as debug_err:
                logger(f"[VTV/Debug] - [ERROR] - ⚠️ Lỗi debug: {debug_err}")
        
            logger("[VTV/DOM] - [REBOOT] - Chưa đủ dữ liệu. Khởi động lại trình duyệt...")
            driver = reboot_driver(driver, vtv_ip, vtv_proto)

    return driver, dom_success, vtv_channels, vtv_master_link

def _vtv_fallback_from_old_file(old_links_dict, logger):
    logger("[VTV/Fallback] - [START] - Đang khôi phục DOM từ file M3U cũ...")
    vtv_channels = []
    for old_name, old_data in old_links_dict.items():
        gn_lower = old_data.get('group', '').lower()
        if 'vtv' in gn_lower or 'địa phương' in gn_lower or 'sctv' in gn_lower:
            # Chặn nhóm bị blacklist ngay cả trong fallback từ file cũ
            if 'vtvcab' in gn_lower or 'one vtv' in gn_lower: continue
            
            # BUSINESS RULE: Tương tự như trên, chỉ sctv là static
            src_type = 'vtvgo_static' if 'sctv' in gn_lower else 'vtvgo_dynamic'
            vtv_channels.append({
                'id': 'fallback', 'name': old_name, 'logo': old_data.get('logo', ''),
                'group_name': old_data.get('group', 'Khác'), 
                'source': src_type, 'original_source': src_type,
                'url': '', 'm3u8_link': None, 'error_msg': None, 'skip': False
            })
    logger(f"[VTV/Fallback] - [SUCCESS] - Đã khôi phục {len(vtv_channels)} kênh VTV từ file.")
    return vtv_channels

def process_vtv_pipeline(old_links_dict, alive_cached, exclude_proxies, vn_proxies, use_auto_proxy, logger):
    logger("\n[VTV/Pipeline] - [INIT] - ====== BẮT ĐẦU CHU TRÌNH VTV ======")
    vtv_channels = []
    vtv_master_link = None
    vtv_proxy_stats = {}
    vtv_ip, vtv_proto = None, "http"
    driver = None
    dom_success = False
    no_proxy_attempts = 0

    while True: 
        vtv_ip, vtv_proto = _get_active_proxy("vtv", alive_cached, exclude_proxies, vn_proxies, use_auto_proxy, logger, vtv_ip, vtv_proto)
        if use_auto_proxy and not vtv_ip: break 
        if not use_auto_proxy:
            if no_proxy_attempts >= 3: break
            no_proxy_attempts += 1

        logger(f"[VTV/Browser] - [START] - Mở trình duyệt (Proxy: {vtv_ip} | {vtv_proto.upper()})")
        driver = reboot_driver(driver, vtv_ip, vtv_proto)
        
        driver, dom_success, vtv_channels, vtv_master_link = _vtv_extract_dom_loop(driver, vtv_ip, vtv_proto, logger)

        if dom_success: 
            if vtv_ip: 
                proxy_key = f"{vtv_proto}://{vtv_ip}"
                vtv_proxy_stats[proxy_key] = vtv_proxy_stats.get(proxy_key, 0) + 1
            break
        
        if vtv_ip:
            logger(f"[VTV/Proxy] - [REJECT] - IP {vtv_ip} thất bại. Loại bỏ và tìm IP khác...")
            exclude_proxies.add(vtv_ip)
            vtv_ip = None 

    if not dom_success:
        vtv_channels = _vtv_fallback_from_old_file(old_links_dict, logger)

    # BUSINESS RULE: Nếu lấy được DOM nhưng mất Link Gốc VTV1, 
    # ta ép kênh VTV1 thành dạng "quét ngầm" và đẩy xuống CUỐI MẢNG.
    if vtv_channels and not vtv_master_link and vtv_channels[0]['source'] != 'fallback_only':
        vtv1_idx = None
        for idx, ch in enumerate(vtv_channels):
            if ch['name'].upper() == "VTV1":
                vtv1_idx = idx
                break
        
        if vtv1_idx is not None:
            logger("[VTV/Pipeline] - [STRATEGY] - Mất Link Gốc VTV1 lúc dạo đầu. Đang đưa VTV1 xuống cuối mảng cào ngầm...")
            vtv1_ch = vtv_channels.pop(vtv1_idx)
            vtv1_ch['source'] = 'vtvgo_dynamic'
            vtv1_ch['skip'] = False
            vtv_channels.append(vtv1_ch)

    if driver and vtv_channels and vtv_channels[0]['source'] != 'fallback_only':
        vtv_dynamic = [ch for ch in vtv_channels if ch['source'] == 'vtvgo_dynamic' and not ch.get('skip')]
        if vtv_dynamic:
            logger(f"[VTV/Pipeline] - [START] - Duyệt ngầm {len(vtv_dynamic)} Kênh...")
            driver = scan_channels_with_rotation(driver, vtv_dynamic, 'vtv', old_links_dict, exclude_proxies, vtv_ip, vtv_proto, vtv_proxy_stats, vn_proxies, use_auto_proxy, logger)
                    
    # Hậu kiểm: Vớt lại Master Link từ VTV1 nếu chiến thuật ép cuối mảng thành công
    if not vtv_master_link and vtv_channels:
        for ch in vtv_channels:
            if ch['name'].upper() == "VTV1" and ch.get('m3u8_link') and ch['source'] != 'fallback_only':
                vtv_master_link = ch['m3u8_link']
                logger("[VTV/Pipeline] - [SUCCESS] - Đã thu hồi Master Link (VTV1) từ cuối mảng quét ngầm thành công!")
                break

    if driver: driver.quit()
    return vtv_master_link, vtv_channels, vtv_proxy_stats

def _tv360_extract_dom_loop(driver, tv360_ip, tv360_proto, logger):
    tv360_channels = []
    dom_success = False
    
    js_extractor_smart = """
        var results = [];
        var sections = document.querySelectorAll('.container-section');
        for (var i = 0; i < sections.length; i++) {
            var h2 = sections[i].querySelector('h2');
            if (!h2) continue;
            var exactGroupName = h2.innerText.trim();
            var gnLower = exactGroupName.toLowerCase();
            if (!gnLower.includes("vĩnh long") && !gnLower.includes("htv") && !gnLower.includes("vtv cab")) continue;

            var links = sections[i].querySelectorAll('a');
            for (var j = 0; j < links.length; j++) {
                var href = links[j].href;
                if (href.includes('/tv/') && href.includes('ch=')) {
                    if (links[j].querySelector('.css-1hssde8')) continue; 
                    var name = links[j].getAttribute('aria-label') || links[j].innerText.trim();
                    var img = links[j].querySelector('img');
                    var logo = '';
                    if (img) {
                        logo = img.getAttribute('src') || '';
                        if (logo.includes('data:image') || logo === '') {
                            var dataSrc = img.getAttribute('data-src') || img.getAttribute('lazy-src');
                            if (dataSrc) {
                                logo = dataSrc;
                            } else if (img.hasAttribute('srcset')) {
                                var srcsetStr = img.getAttribute('srcset');
                                var firstUrl = srcsetStr.split(',')[0].trim().split(' ')[0];
                                if (firstUrl && !firstUrl.includes('data:image')) logo = firstUrl;
                            }
                        }
                    }
                    try {
                        var urlObj = new URL(href);
                        results.push({
                            id: urlObj.searchParams.get('ch'), slug: urlObj.pathname.split('/').pop(),
                            name: name || urlObj.pathname.split('/').pop(), logo: logo,
                            group_name: exactGroupName, link: href
                        });
                    } catch(e) {}
                }
            }
        }
        var unique = [];
        var ids = new Set();
        for(var ch of results){
            if(ch.id && !ids.has(ch.id)){ ids.add(ch.id); unique.push(ch); }
        }
        return unique;
    """

    for t in [60, 120, 240]:
        logger(f"[TV360/DOM] - [START] - Đang tải danh sách kênh (Timeout: {t}s)")
        try:
            driver.set_page_load_timeout(t)
            driver.get("https://tv360.vn/tv")
            
            # WORKAROUND: Kiểm tra trang báo lỗi do mạng sập
            page_src = driver.page_source
            if "ERR_CONNECTION" in page_src or "ERR_PROXY" in page_src or "ERR_TIMED_OUT" in page_src:
                logger("[TV360/DOM] - [FAILED] - 🚨 Proxy Dead: Trình duyệt báo lỗi mạng")
                raise Exception("Proxy Dead")
                
            time.sleep(3) 
            
            driver.execute_script("""
                var totalHeight = 0; var distance = 600;
                var timer = setInterval(() => {
                    var scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    if(totalHeight >= scrollHeight) clearInterval(timer);
                }, 250);
            """)
            time.sleep(4) 
            
            dom_list = driver.execute_script(js_extractor_smart)
            if dom_list:
                for c in dom_list:
                    tv360_channels.append({
                        'id': str(c.get('id')), 'name': c.get('name'), 'logo': c.get('logo', ''),
                        'group_name': c.get('group_name'), 
                        'source': 'tv360_dynamic', 'original_source': 'tv360_dynamic',
                        'url': c.get('link'), 'm3u8_link': None, 'error_msg': None, 'skip': False
                    })
                dom_success = True
                logger(f"[TV360/DOM] - [SUCCESS] - Lấy được {len(tv360_channels)} kênh miễn phí.")
                break
        except Exception as e: 
            logger(f"[TV360/DOM] - [ERROR] - Ngoại lệ: {e}")
        
        logger("[TV360/DOM] - [REBOOT] - Lỗi/Timeout. Khởi động lại trình duyệt...")
        driver = reboot_driver(driver, tv360_ip, tv360_proto)
        
    return driver, dom_success, tv360_channels

def _tv360_fallback_from_old_file(old_links_dict, logger):
    logger("[TV360/Fallback] - [START] - Đang khôi phục DOM từ file M3U cũ...")
    tv360_channels = []
    for old_name, old_data in old_links_dict.items():
        gn_lower = old_data.get('group', '').lower()
        if 'vĩnh long' in gn_lower or 'thvl' in gn_lower or 'htv' in gn_lower or 'vtv cab' in gn_lower or 'vtvcab' in gn_lower:
            tv360_channels.append({
                'id': 'fallback', 'name': old_name, 'logo': old_data.get('logo', ''),
                'group_name': old_data.get('group', 'Khác'), 
                'source': 'tv360_dynamic', 'original_source': 'tv360_dynamic',
                'url': '', 'm3u8_link': None, 'error_msg': None, 'skip': False
            })
    logger(f"[TV360/Fallback] - [SUCCESS] - Đã khôi phục {len(tv360_channels)} kênh TV360 từ file.")
    return tv360_channels

def process_tv360_pipeline(old_links_dict, alive_cached, exclude_proxies, vn_proxies, use_auto_proxy, logger):
    logger("\n[TV360/Pipeline] - [INIT] - ====== BẮT ĐẦU CHU TRÌNH TV360 ======")
    tv360_channels = []
    tv360_proxy_stats = {}
    tv360_ip, tv360_proto = None, "http"
    driver = None
    dom_success = False
    no_proxy_attempts = 0

    while True:
        tv360_ip, tv360_proto = _get_active_proxy("tv360", alive_cached, exclude_proxies, vn_proxies, use_auto_proxy, logger, tv360_ip, tv360_proto)
        if not tv360_ip and use_auto_proxy: break
        if not use_auto_proxy:
            if no_proxy_attempts >= 3: break
            no_proxy_attempts += 1

        logger(f"[TV360/Browser] - [START] - Mở trình duyệt (Proxy: {tv360_ip} | {tv360_proto.upper()})")
        driver = reboot_driver(driver, tv360_ip, tv360_proto)
        
        driver, dom_success, tv360_channels = _tv360_extract_dom_loop(driver, tv360_ip, tv360_proto, logger)
            
        if dom_success: break
        
        if tv360_ip:
            logger(f"[TV360/Proxy] - [REJECT] - IP {tv360_ip} thất bại. Loại bỏ và tìm IP khác...")
            exclude_proxies.add(tv360_ip)
            tv360_ip = None

    if not dom_success:
        tv360_channels = _tv360_fallback_from_old_file(old_links_dict, logger)
    else:
        for c in tv360_channels:
            if c['logo'].startswith('data:image') or not c['logo']:
                if c['name'] in old_links_dict and old_links_dict[c['name']].get('logo'):
                    c['logo'] = old_links_dict[c['name']]['logo']

    if driver and tv360_channels and tv360_channels[0]['source'] != 'fallback_only':
        channels_to_scan = [ch for ch in tv360_channels if ch['source'] == 'tv360_dynamic']
        if channels_to_scan:
            logger(f"[TV360/Pipeline] - [START] - Duyệt ngầm {len(channels_to_scan)} Kênh TV360...")
            driver = scan_channels_with_rotation(driver, channels_to_scan, 'tv360', old_links_dict, exclude_proxies, tv360_ip, tv360_proto, tv360_proxy_stats, vn_proxies, use_auto_proxy, logger)
        
    if driver: driver.quit()
    return tv360_channels, tv360_proxy_stats