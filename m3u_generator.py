import os
import re
import requests

def load_old_m3u_links(filepath):
    old_links = {}
    if not os.path.exists(filepath): return old_links
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        current_name, current_group, current_logo = None, "Khác", ""
        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF"):
                parts = line.split(',')
                if len(parts) > 1: 
                    current_name = parts[-1].strip()
                    match_group = re.search(r'group-title="(.*?)"', line)
                    if match_group: current_group = match_group.group(1)
                    match_logo = re.search(r'tvg-logo="(.*?)"', line)
                    if match_logo: current_logo = match_logo.group(1)
            elif line and not line.startswith("#") and current_name:
                old_links[current_name] = {'url': line, 'group': current_group, 'logo': current_logo}
                current_name, current_group, current_logo = None, "Khác", ""
    except: pass
    return old_links

def remove_accents(input_str):
    s1 = u'ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúýĂăĐđĨĩŨũƠơƯưẠạẢảẤấẦầẨẩẪẫẬậẮắẰằẲẳẴẵẶặẸẹẺẻẼẽẾếỀềỂểỄễỆệỈỉỊịỌọỎỏỐốỒồỔổỖỗỘộỚớỜờỞởỠỡỢợỤụỦủỨứỪừỬửỮữỰựỲỳỴỵỶỷỸỹ'
    s0 = u'AAAAEEEIIOOOOUUYaaaaeeeiioooouuyAaDdIiUuOoUuAaAaAaAaAaAaAaAaAaAaAaAaEeEeEeEeEeEeEeEeIiIiOoOoOoOoOoOoOoOoOoOoOoOoUuUuUuUuUuUuUuYyYyYyYy'
    s = ''
    for c in input_str:
        if c in s1: s += s0[s1.index(c)]
        else: s += c
    return s

def get_vtv_acronym(ch_name):
    clean_name = remove_accents(ch_name).lower().replace('-', ' ')
    if "vietnam today" in clean_name or "viet nam today" in clean_name:
        return "vietnamtoday"
    words = clean_name.split()
    if not words: return ""
    res = words[0] 
    for w in words[1:]:
        if w.isdigit(): res += w
        else: res += w[0] 
    return res
    
def create_slug(ch_name):
    text = remove_accents(ch_name).lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'\s+', '-', text).strip('-')
    return text

def check_link_is_alive(url, proxy_ip=None, protocol="http"):
    headers = {"User-Agent": "VLC/3.0.16 LibVLC/3.0.16"}
    proxies = None
    if proxy_ip:
        proxies = {"http": f"{protocol}://{proxy_ip}", "https": f"{protocol}://{proxy_ip}"}
    try:
        response = requests.get(url, headers=headers, proxies=proxies, timeout=5, stream=True)
        return response.status_code == 200
    except Exception:
        return False

def find_proxy_for_streaming(master_link, exclude_set, vn_proxies, logger):
    logger("   [Ping Nội Suy] 🔄 Đang tìm Proxy có khả năng Stream (Ping test nghiệm thu VTV)...")
    for proxy_data in vn_proxies:
        ip_port = proxy_data['ip']
        protocol = proxy_data['protocol']
        if ip_port in exclude_set: continue

        if check_link_is_alive(master_link, ip_port, protocol):
            logger(f"      -> ✅ Bắt được Proxy Stream tốt: {ip_port} ({protocol.upper()})")
            return ip_port, protocol
    return None, "http"

def verify_sctv_link_with_proxy(target_link, master_link, ch_name, proxy_state, vn_proxies, use_auto_proxy, logger):
    logger(f"   [Kiểm tra SCTV] Đang test kênh {ch_name}...")
    link_is_ok = False
    
    if not use_auto_proxy:
        link_is_ok = check_link_is_alive(target_link)
        if not link_is_ok: logger(f"      -> ❌ Link {ch_name} trả về HTTP Lỗi (Đã chết).")
    else:
        for attempt in range(3): 
            if not proxy_state['ip']:
                proxy_state['ip'], proxy_state['proto'] = find_proxy_for_streaming(master_link, proxy_state['banned'], vn_proxies, logger)

            if not proxy_state['ip']:
                logger("      ⚠️ Cạn kiệt Proxy Stream! Giữ lại link để đảm bảo an toàn.")
                link_is_ok = True 
                break

            is_alive = check_link_is_alive(target_link, proxy_state['ip'], proxy_state['proto'])
            if is_alive:
                link_is_ok = True
                break
            else:
                proxy_health_check = check_link_is_alive(master_link, proxy_state['ip'], proxy_state['proto'])
                if proxy_health_check:
                    logger(f"      -> ❌ Link {ch_name} thực sự đã CHẾT (Proxy vẫn sống).")
                    link_is_ok = False
                    break
                else:
                    logger(f"      -> ⚠️ Proxy {proxy_state['ip']} đã bị Block. Đang vứt bỏ và tìm proxy mới...")
                    proxy_state['banned'].add(proxy_state['ip'])
                    proxy_state['ip'] = None
                    
    return link_is_ok

def generate_m3u(vtv_master_link, master_channels_list, file_path, vn_proxies, use_auto_proxy, logger):
    official_vtv_group = "Kênh VTV" 
    for ch in master_channels_list:
        if ch['name'].upper() == "VTV1":
            official_vtv_group = ch['group_name']
            break

    for ch in master_channels_list:
        ch_name_lower = ch['name'].lower()
        if any(keyword in ch_name_lower for keyword in ['an ninh', 'antv', 'quốc phòng', 'qptv', 'công an nhân dân', 'cand']):
            ch['group_name'] = official_vtv_group

    def get_group_priority(group_name):
        gn_lower = group_name.lower()
        if 'vtv cab' in gn_lower or 'vtvcab' in gn_lower: return 2
        if 'vtv' in gn_lower: return 1
        if 'htv' in gn_lower: return 3
        if 'vĩnh long' in gn_lower or 'thvl' in gn_lower or 'ttvl' in gn_lower: return 4
        if 'địa phương' in gn_lower or 'dia phuong' in gn_lower: return 5
        if 'sctv' in gn_lower: return 99 
        return 6 

    # FIX: VTV1 có thể bị trôi xuống cuối mảng khi hệ thống dời kênh này xuống quét ngầm.
    # Sử dụng tuple 2 chiều (Group Priority, Channel Priority) để cưỡng ép VTV1 LUÔN LUÔN đứng đầu (chỉ số 0) trong nhóm của nó.
    master_channels_list.sort(key=lambda x: (
        get_group_priority(x['group_name']),
        0 if x['name'].strip().upper() == 'VTV1' else 1
    ))

    # BUSINESS RULE: 1. THU THẬP KHUÔN MẪU (Chỉ lấy kênh VTV Trung Ương)
    vtv_master_pool = []
    if vtv_master_link:
        vtv_master_pool.append({'name': 'VTV 1', 'link': vtv_master_link})
        
    for ch in master_channels_list:
        if ch.get('m3u8_link') and ch['source'] == 'vtvgo_dynamic':
            # Chỉ lấy các kênh thuộc official_vtv_group (Loại bỏ triệt để nhóm Địa Phương)
            if ch['group_name'] == official_vtv_group:
                if not any(x['link'] == ch['m3u8_link'] for x in vtv_master_pool):
                    vtv_master_pool.append({'name': ch['name'], 'link': ch['m3u8_link']})

    # BUSINESS RULE: 2. BỘ NHỚ TRÍ TUỆ NHÂN TẠO (Đọc file Cache)
    golden_cache_file = "sctv_golden_cache.txt"
    cached_vtv_name = None
    try:
        if os.path.exists(golden_cache_file):
            with open(golden_cache_file, "r", encoding="utf-8") as f:
                cached_vtv_name = f.read().strip()
                if cached_vtv_name:
                    logger(f"\n   [System] Đọc bộ nhớ: Lần trước {cached_vtv_name} là Khuôn chuẩn. Sẽ thử lại ưu tiên số 1.")
    except: pass

    # Tái sắp xếp ưu tiên Khuôn mẫu dựa vào bộ nhớ
    if cached_vtv_name:
        for i, item in enumerate(vtv_master_pool):
            if item['name'] == cached_vtv_name:
                vtv_master_pool.insert(0, vtv_master_pool.pop(i))
                break

    # Trạng thái Proxy dùng chung cho toàn bộ pha Hiệu chuẩn và Nội suy thực tế
    proxy_state = {'ip': None, 'proto': 'http', 'banned': set()}
    golden_template_link = None
    
    # BUSINESS RULE: 3. PHA HIỆU CHUẨN CHÉO (Thử nghiệm trên 3 kênh SCTV để tìm Khuôn chuẩn)
    sctv_subjects = [c for c in master_channels_list if c['source'] in ('vtvgo_static', 'fallback_only') and not c.get('skip') and ('sctv' in c['group_name'].lower() or 'sctv' in c['name'].lower())][:3]
    
    if sctv_subjects and vtv_master_pool:
        logger(f"\n   [System] Đang tìm nghiệm Khuôn mẫu chuẩn cho SCTV từ {len(vtv_master_pool)} kênh VTV Trung Ương...")
        for template in vtv_master_pool:
            template_name = template['name']
            template_link = template['link']
            logger(f"      -> Thử Khuôn mẫu: {template_name}...")
            
            is_valid = False
            for subj in sctv_subjects:
                folder_id = get_vtv_acronym(subj['name'])
                token_match = re.search(r'\.vn/([^/]+/[^/]+)/', template_link)
                if token_match:
                    tokens = token_match.group(1)
                    test_link = f"https://vtvgolive-sctvdrm.vtvdigital.vn/{tokens}/manifest/{folder_id}/master.m3u8"
                else:
                    test_link = re.sub(r'(/manifest/|/live/)[^/]+(/)', f'\\g<1>{folder_id}\\g<2>', template_link)
                
                # Chỉ cần 1 kênh SCTV test thành công là chốt sổ ngay lập tức
                if verify_sctv_link_with_proxy(test_link, template_link, subj['name'], proxy_state, vn_proxies, use_auto_proxy, logger):
                    is_valid = True
                    break
                    
            if is_valid:
                golden_template_link = template_link
                logger(f"      ✅ TÌM RA KHUÔN CHUẨN: {template_name} (Sẽ dùng duy nhất khuôn này cho tất cả SCTV)")
                # BUSINESS RULE: 4. GHI ĐÈ BỘ NHỚ (Cập nhật lại tên Kênh Vàng cho lần sau)
                try:
                    with open(golden_cache_file, "w", encoding="utf-8") as f:
                        f.write(template_name)
                except: pass
                break
            else:
                logger(f"      -> ⚠️ Khuôn {template_name} không phù hợp. Đang chuyển khuôn khác...")
                
        if not golden_template_link:
            logger(f"      -> ❌ BÓ TAY: Toàn bộ {len(vtv_master_pool)} Khuôn mẫu VTV đều không mở được SCTV.")

    m3u_content = "#EXTM3U\n"
    for ch in master_channels_list:
        if ch.get('skip') and not ch.get('m3u8_link'): continue 
        
        ch_name = ch['name']
        group_name = ch['group_name']
        extinf_line = f'#EXTINF:-1 tvg-id="{ch_name}" tvg-logo="{ch["logo"]}" group-title="{group_name}", {ch_name}\n'
        
        if ch.get('m3u8_link') and ch['source'] != 'fallback_only':
             m3u_content += extinf_line
             m3u_content += f"{ch['m3u8_link']}\n"
             continue

        # BUSINESS RULE: 5. TRIỂN KHAI HÀNG LOẠT (Dùng Khuôn Vàng áp dụng cho SCTV)
        if ch['source'] == 'vtvgo_static':
            if not golden_template_link: 
                # Không có Khuôn Vàng => Bỏ qua không in kênh lỗi này ra file
                continue
            
            folder_id = get_vtv_acronym(ch_name)
            token_match = re.search(r'\.vn/([^/]+/[^/]+)/', golden_template_link)
            if token_match:
                tokens = token_match.group(1)
                new_link = f"https://vtvgolive-sctvdrm.vtvdigital.vn/{tokens}/manifest/{folder_id}/master.m3u8"
            else:
                new_link = re.sub(r'(/manifest/|/live/)[^/]+(/)', f'\\g<1>{folder_id}\\g<2>', golden_template_link)
            
            # Vì Khuôn Vàng đã được chứng minh là đúng chuẩn 100%, nếu Ping fail thì kênh đã chết server thật sự.
            if verify_sctv_link_with_proxy(new_link, golden_template_link, ch_name, proxy_state, vn_proxies, use_auto_proxy, logger):
                m3u_content += extinf_line
                m3u_content += f"{new_link}\n"
            else:
                logger(f"      -> ❌ Kênh {ch_name} đã CHẾT THẬT SỰ trên Server (Đã loại bỏ khỏi danh sách).")
                
        elif ch['source'] in ('vtvgo_dynamic', 'tv360_dynamic'):
            error_info = ch.get('error_msg', 'Không rõ')
            m3u_content += extinf_line
            m3u_content += f"# Lỗi: {error_info} | Link test: {ch['url']}\n"
        
        elif ch['source'] == 'fallback_only':
            is_sctv = 'sctv' in group_name.lower() or 'sctv' in ch_name.lower()
            if is_sctv and golden_template_link:
                # Vẫn dùng Khuôn Vàng để kiểm tra Proxy Ping cho Fallback
                if verify_sctv_link_with_proxy(ch['m3u8_link'], golden_template_link, ch_name, proxy_state, vn_proxies, use_auto_proxy, logger):
                    m3u_content += extinf_line
                    m3u_content += f"{ch['m3u8_link']}\n"
            else:
                m3u_content += extinf_line
                m3u_content += f"{ch['m3u8_link']}\n"
        
    try:
        logger(f"\n   [Debug] Đang tiến hành ghi file vào đường dẫn: {file_path}")
        dir_name = os.path.dirname(file_path)
        if dir_name: 
            os.makedirs(dir_name, exist_ok=True)
            
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(m3u_content)
        logger(f"🎉 HOÀN TẤT! Đã xuất file M3U Hỗn hợp thành công.")
    except Exception as e:
        logger(f"❌ LỖI Ghi file: {e}")