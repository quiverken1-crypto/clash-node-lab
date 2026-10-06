"""Group only names with recognisable countries; retain route prefixes."""
import re
COUNTRIES = {
 'HK':['香港','hong kong','hongkong'], 'TW':['台湾','台灣','taiwan'], 'US':['美国','美國','united states','usa'],
 'JP':['日本','japan'], 'SG':['新加坡','singapore'], 'DE':['德国','德國','germany'], 'GB':['英国','英國','united kingdom'],
 'CA':['加拿大','canada'], 'AU':['澳大利亚','澳大利亞','澳洲','australia'], 'FR':['法国','法國','france'],
 'NL':['荷兰','荷蘭','netherlands'], 'IN':['印度','india'], 'VN':['越南','vietnam'], 'MY':['马来西亚','馬來西亞','malaysia'],
 'TH':['泰国','泰國','thailand'], 'KR':['韩国','韓國','korea'], 'ID':['印尼','印度尼西亚','indonesia'], 'TR':['土耳其','turkey'],
 'EG':['埃及','egypt'], 'KH':['柬埔寨','cambodia'], 'NG':['尼日利亚','nigeria'], 'PK':['巴基斯坦','pakistan'],
 'PL':['波兰','波蘭','poland'], 'MO':['澳门','澳門','macau'], 'RU':['俄罗斯','俄羅斯','russia'], 'PH':['菲律宾','菲律賓','philippines'],
 'AR':['阿根廷','argentina'], 'CH':['瑞士','switzerland'], 'FI':['芬兰','finland'], 'SE':['瑞典','sweden'], 'IT':['意大利','italy'],
 'ES':['西班牙','spain'], 'BR':['巴西','brazil'], 'AE':['阿联酋','uae'], 'ZA':['南非','south africa']
}

def classify(node):
    name=node.get('name',''); text=re.sub(r'[\U0001f000-\U0001ffff\u2600-\u27ff\ufe0f]','',name).strip()
    hits=[]
    for code,aliases in COUNTRIES.items():
        for alias in aliases:
            m=re.search(re.escape(alias),text,re.I)
            if m:hits.append((m.start(),-len(alias),code,m))
    country='未知'; cut=None
    if hits:
        _,_,country,match=min(hits,key=lambda h:(h[0],h[1]));cut=match.start()
    else:
        flag=re.search(r'([\U0001f1e6-\U0001f1ff]{2})',name)
        if flag:
            country=''.join(chr(ord(c)-0x1f1e6+65) for c in flag.group(1))
            if country=='UM':country='US'
    m=re.search(r'(\d+(?:\.\d+)?)\s*[xX倍]|[xX]\s*(\d+(?:\.\d+)?)',text)
    multiplier=format(float(next(g for g in m.groups() if g)), 'g') if m else '1'
    protocol=str(node.get('type','未知')).lower()
    protocol={'shadowsocks':'ss','shadowsocksr':'ssr','socks':'socks5'}.get(protocol,protocol)
    prefix=text[:cut] if cut is not None else text
    prefix=re.sub(r'\b(?:vless|vmess|trojan|hysteria2?|ssr?|tuic|socks5?|http)\b','',prefix,flags=re.I)
    prefix=re.sub(r'\d+(?:\.\d+)?\s*[xX倍]|[xX]\s*\d+(?:\.\d+)?','',prefix)
    prefix=re.sub(r'[\s\[\]【】()（）_—–-]+',' ',prefix).strip() or '默认线路'
    # Unknown names/flag-only routes remain distinct to avoid accidental merging.
    if country=='未知' or cut is None:prefix=text
    key=f'{prefix}|{country}|{multiplier}|{protocol}'
    return {'group_key':key,'prefix':prefix,'country':country,'multiplier':multiplier,'protocol':protocol,
            'group_label':f'{prefix} / {country} / {multiplier}x / {protocol}'}

def group_nodes(nodes):
    groups={}
    for node in nodes:
        info=classify(node);key=info['group_key']
        if key not in groups:groups[key]={**info,'members':[],'representative':node['name']}
        groups[key]['members'].append(node['name'])
    return list(groups.values())

def representatives(nodes):
    lookup={n['name']:n for n in nodes};chosen=[]
    for group in group_nodes(nodes):
        node=dict(lookup[group['representative']]);node['_group']=group;chosen.append(node)
    return chosen
