# -*- coding: utf-8 -*-
"""
标书脱敏核心引擎 v3（纯本地，数据不出本机）

识别层：
  1. 用户词库 / 补充敏感词（最高优先级）
  2. 正则硬规则：邮箱、身份证、手机/座机、银行卡/账号、统一社会信用代码、
     报价金额（含大写金额）、项目编号/代号、地址、公司/单位、职务后的人名
  3. 本地智能识别：jieba 词性标注（可选，纯本地，无需联网）补人名/公司
  4. 白名单：明确不脱的词，优先级最高

替换层：
  - placeholder 占位符：【公司A】【人名A】…… 可读、可靠、可逆（推荐）
  - realistic   拟真伪装：生成格式一致的虚构值，同样可逆
  - star        星号遮盖（不可逆）
  - delete      严格删除（不可逆）

使用（Web 工作台）：
    python webapp.py
命令行：
    python redactor.py redact 样本标书.docx --vocab vocab.txt
    python redactor.py redact 样本标书.docx --mode realistic --password 口令
    python redactor.py restore 样本标书_脱敏.docx --mapping 样本标书_映射.enc --password 口令 -o 还原.docx
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    HAS_CRYPTO = False
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore


# =====================================================================
# 一、类别定义
# =====================================================================
CATEGORIES = {
    "COMPANY": "公司/单位",
    "PERSON": "人名",
    "PHONE": "电话",
    "IDCARD": "身份证",
    "BANK": "银行卡/账号",
    "EMAIL": "邮箱",
    "ADDRESS": "地址",
    "AMOUNT": "报价/金额",
    "CODE": "项目代号",
    "CREDITCODE": "信用代码",
}
CAT_CN = CATEGORIES
CAT_ORDER = list(CATEGORIES)
ZH2EN = {
    "公司": "COMPANY", "单位": "COMPANY", "机构": "COMPANY", "公司名": "COMPANY",
    "人名": "PERSON", "姓名": "PERSON", "人员": "PERSON",
    "电话": "PHONE", "手机": "PHONE", "联系方式": "PHONE",
    "身份证": "IDCARD", "证件": "IDCARD", "证件号": "IDCARD",
    "银行": "BANK", "银行卡": "BANK", "账号": "BANK", "帐号": "BANK", "账户": "BANK",
    "邮箱": "EMAIL", "邮件": "EMAIL", "电子邮件": "EMAIL",
    "地址": "ADDRESS", "住址": "ADDRESS", "住所": "ADDRESS",
    "报价": "AMOUNT", "金额": "AMOUNT", "价格": "AMOUNT", "总价": "AMOUNT",
    "代号": "CODE", "编号": "CODE", "项目代号": "CODE", "项目编号": "CODE",
    "信用代码": "CREDITCODE", "统一社会信用代码": "CREDITCODE",
    "白名单": "WHITELIST", "WHITELIST": "WHITELIST",
}

STRATEGY_CN = {"fake": "假名替换", "star": "星号遮盖", "delete": "严格删除"}
STRATEGY_ALIAS = {
    "fake": "fake", "假名": "fake", "占位符": "fake", "拟真": "fake",
    "placeholder": "fake", "realistic": "fake", "replace": "fake",
    "star": "star", "星号": "star", "遮盖": "star",
    "delete": "delete", "删除": "delete", "抹除": "delete", "严格删除": "delete",
}

MODE_CN = {"placeholder": "占位符", "realistic": "拟真伪装"}
LOGGER = logging.getLogger(__name__)


def parse_category(value: str, default: str = "COMPANY") -> str:
    value = (value or "").strip()
    if not value:
        return default
    key = value.upper()
    if key in CATEGORIES:
        return key
    return ZH2EN.get(value, default)


# =====================================================================
# 二、基础校验工具
# =====================================================================
def luhn_ok(num: str) -> bool:
    digits = re.sub(r"\D", "", num)
    if not 13 <= len(digits) <= 19:
        return False
    total, dbl = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if dbl:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        dbl = not dbl
    return total % 10 == 0


def luhn_check_digit(prefix: str) -> str:
    """给前 N-1 位补 Luhn 校验位"""
    total, dbl = 0, True
    for ch in reversed(prefix):
        d = int(ch)
        if dbl:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        dbl = not dbl
    return str((10 - total % 10) % 10)


def idcard_ok(num: str) -> bool:
    if len(num) == 15 and num.isdigit():
        return True
    if len(num) != 18:
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"
    try:
        return check[sum(int(a) * b for a, b in zip(num[:17], weights)) % 11] == num[-1].upper()
    except ValueError:
        return False


def idcard_check_digit(body17: str) -> str:
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"
    return check[sum(int(a) * b for a, b in zip(body17, weights)) % 11]


USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def uscc_ok(code: str) -> bool:
    if len(code) != 18 or any(c not in USCC_CHARS for c in code):
        return False
    total = sum(USCC_CHARS.index(c) * w for c, w in zip(code[:17], USCC_WEIGHTS))
    expected = USCC_CHARS[(31 - total % 31) % 31]
    return code[-1] == expected


def uscc_check_digit(body17: str) -> str:
    total = sum(USCC_CHARS.index(c) * w for c, w in zip(body17, USCC_WEIGHTS))
    return USCC_CHARS[(31 - total % 31) % 31]


def tag_letter(i: int) -> str:
    """0→A, 25→Z, 26→AA"""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    i += 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        out = letters[rem] + out
    return out


def _occurrence_key(category: str, text: str, record_index: int, start: int) -> str:
    raw = f"{category}\0{text}\0{record_index}\0{start}".encode("utf-8")
    return "occ:" + hashlib.sha256(raw).hexdigest()[:20]


def _paragraph_location_key(paragraph: Paragraph) -> str:
    try:
        root = paragraph._p.getroottree()
        return root.getpath(paragraph._p)
    except Exception:
        return f"node:{paragraph._p}"


def _docx_occurrence_indices(doc) -> dict[str, int]:
    indices: dict[str, int] = {}
    for index, paragraph in enumerate(iter_paragraphs(doc)):
        indices[_paragraph_location_key(paragraph)] = index
    return indices


def _assign_occurrence_keys(hits: list[Hit], text: str, record_index: int) -> list[Hit]:
    for hit in hits:
        hit.occurrence_key = _occurrence_key(
            hit.category, hit.text, record_index, hit.start)
    return hits


# =====================================================================
# 三、词库 / 补充敏感词
# =====================================================================
def parse_word_spec(spec: str) -> tuple[list[tuple[str, str]], list[str]]:
    """解析 `类别|词` 多行文本，返回 (词库, 白名单)。类别可中英文。"""
    vocab: list[tuple[str, str]] = []
    whitelist: list[str] = []
    for raw in (spec or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            cat, word = line.split("|", 1)
        elif "｜" in line:
            cat, word = line.split("｜", 1)
        else:
            cat, word = "COMPANY", line
        cat = parse_category(cat.strip(), "COMPANY")
        word = word.strip()
        if not word:
            continue
        if cat == "WHITELIST":
            whitelist.append(word)
        else:
            vocab.append((word, cat))
    # 长词优先，避免「中国银行」先于「中国银行北京分行」命中
    vocab.sort(key=lambda x: len(x[0]), reverse=True)
    whitelist.sort(key=len, reverse=True)
    return vocab, whitelist


def load_vocab(path: Optional[Path]) -> tuple[list[tuple[str, str]], list[str]]:
    if not path:
        return [], []
    try:
        return parse_word_spec(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []


# =====================================================================
# 四、识别层
# =====================================================================
@dataclass
class Hit:
    start: int
    end: int
    category: str
    text: str
    source: str = "规则"
    occurrence_key: str = ""

    @property
    def key(self) -> str:
        return f"{self.category}|{self.text}"


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IDCARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx]|\d{15})(?!\d)")
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
LANDLINE_RE = re.compile(
    r"(?<!\d)(?:0\d{2,3}[-－—]?\d{7,8}(?:[-－—]\d{1,6})?"
    r"|400[-－—]?\d{3}[-－—]?\d{4}|800[-－—]?\d{3}[-－—]?\d{4})(?!\d)")
BANK_CARD_RE = re.compile(r"(?<![\d])(?:\d[ -]?){13,19}(?![\d])")
BANK_LABEL_RE = re.compile(
    r"(?:银行账号|银行帐号|开户账号|开户帐号|银行账户|银行帐户|账号|帐号|卡号|账户|帐户)"
    r"[ \t]*[:：]?[ \t]*([0-9][0-9 \t\-－]{8,26})")
USCC_RE = re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])")
CURRENCY_AMOUNT_RE = re.compile(
    r"(?:[￥¥]|(?:人民币|RMB))\s?(?:\d{1,3}(?:[,，]\d{3})+|\d+)"
    r"(?:\.\d{1,2})?[ \t]*(?:万元|万|元)?")
AMOUNT_LABEL_RE = re.compile(
    r"(?:投标报价|报价|总报价|合同价|合同总价|中标价|成交价|总价|金额|保证金|预算|控制价|限价|单价)"
    r"[^\d\n]{0,12}(\d{1,3}(?:[,，]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"[ \t]*(万元|万|元)?")
CN_NUM = "零壹贰叁肆伍陆柒捌玖拾佰仟万亿"
CN_AMOUNT_RE = re.compile(rf"(?:人民币)?[{CN_NUM}]{{2,}}(?:元|圆)[{CN_NUM}角分]*整?")
CODE_LABEL_RE = re.compile(
    r"(?:项目|招标|采购|合同|内部|工程|文件|标段)?(?:编号|代号|编码|标号|项目号)"
    r"\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9][\u4e00-\u9fa5A-Za-z0-9\-_/—－.（）()]{2,30})")
ALNUM_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2,12}[-_/]?\d{2,10}(?:[-_/]\d{1,10})?|[A-Z]{2,8}\d{2,10})(?![A-Za-z0-9])")
PROJECT_NAME_RE = re.compile(
    r"(?:项目名称|工程名称|标段名称|项目全称)\s*[:：]\s*([^\s，。；;：:]{2,40})")
PROJECT_GENERIC = {
    "本项目", "该项目", "本工程", "该工程", "工程项目", "建设项目", "施工项目",
    "招标项目", "投标项目", "采购项目", "工程项目", "上述项目", "本标段", "该标段",
}

_ADDR_CHAR = r"\u4e00-\u9fa5A-Za-z0-9"
_PROVINCES = (
    "北京|上海|天津|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|"
    "河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|"
    "宁夏|新疆|香港|澳门"
)
ADDRESS_RE = re.compile(
    rf"(?:(?:{_PROVINCES})(?:省|市|自治区|特别行政区)?)?"
    rf"(?:[{_ADDR_CHAR}]{{2,8}}(?:市|自治州|地区|盟))?"
    rf"(?:[{_ADDR_CHAR}]{{2,8}}(?:区|县|旗|市))?"
    rf"(?:[{_ADDR_CHAR}]{{2,20}}(?:路|街|大道|道|巷|弄|胡同|里))"
    rf"(?:[{_ADDR_CHAR}]{{0,10}}\d{{1,5}}(?:号|弄))?"
    rf"(?:[{_ADDR_CHAR}]{{0,10}}(?:号楼|栋|单元|层|室))?"
)
ADDRESS_LABEL_RE = re.compile(
    r"(?:注册地址|办公地址|联系地址|通讯地址|地址|住址|住所|办公地点|所在地)"
    r"\s*[:：]\s*([^\s，。；;：:]{6,60})")

_COMPANY_CHARS = r"\u4e00-\u9fa5A-Za-z0-9（）()·&"
COMPANY_SUFFIXES = [
    "股份有限公司", "有限责任公司", "集团有限公司", "有限公司", "集团公司",
    "分公司", "子公司", "集团", "公司", "工程局", "建设局", "管理局",
    "研究院", "设计院", "事务所", "银行", "分行", "支行", "营业部",
    "信用社", "合作社", "大学", "学院", "医院", "中心", "厂", "店",
]
COMPANY_SUFFIX_RE = re.compile("|".join(
    sorted((re.escape(s) for s in COMPANY_SUFFIXES), key=len, reverse=True)))
COMPANY_GENERIC = {
    "本公司", "我公司", "贵公司", "该公司", "公司", "分公司", "子公司",
    "有限公司", "有限责任公司", "股份有限公司", "集团有限公司", "集团公司",
    "集团", "项目部", "指挥部", "监理单位", "建设单位", "施工单位", "设计单位",
    "勘察单位", "咨询单位", "供应商", "投标人", "招标人", "采购人", "中标人",
    "甲方", "乙方", "单位", "工程", "项目", "中心", "银行", "分行", "支行", "营业部",
    "开户银行", "开户行", "银行名称", "账户名称", "收款单位", "付款单位",
    "招标代理机构", "代理机构", "采购单位", "招标单位",
}
COMPANY_PREFIX_NOISE = [
    "投标人", "招标人", "采购人", "中标人", "供应商", "分包单位", "监理单位",
    "建设单位", "施工单位", "设计单位", "勘察单位", "咨询单位", "单位名称",
    "公司名称", "开户银行", "开户行", "收款单位", "付款单位", "开户单位",
    "本工程", "本项目", "项目", "工程", "单位", "公司",
]
COMPANY_BOUNDARY = set("，。；：、,.;:!?！？（）()《》【】[]{}<>“”\"'‘’ \t\n\r由为是在经与和及向对将的了把被从到给让使以并且或等各本该我贵其")
COMPANY_NAME_CHARS = set("（）()·&-_")

SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章"
    "云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常"
    "乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅"
    "庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡"
    "凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴"
    "陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰"
    "秋仲伊宫宁仇栾暴甘斜厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍"
    "赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕"
    "冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国"
    "文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢"
    "关蒯相查后荆红游竺权逯盖益桓公"
)
COMPOUND_SURNAMES = "欧阳|司马|上官|诸葛|东方|皇甫|尉迟|公孙|慕容|令狐|司徒|夏侯|长孙|宇文|轩辕|端木|独孤|南宫|西门|百里|呼延"
PERSON_ROLE_RE = re.compile(
    r"(?:法定代表人|法人代表|项目经理|项目负责人|技术负责人|安全负责人|质量负责人|"
    r"总工程师|项目总监|授权代表|委托代理人|投标人代表|联系人|负责人|安全员|质量员|"
    r"施工员|资料员|预算员|造价员|经办人|编制人|审核人|批准人|签字人|代理人|"
    r"姓名|法人)"
    r"\s*[:：]\s*([\u4e00-\u9fa5·]{2,4})")
NAME_STANDALONE_RE = re.compile(
    rf"(?<![\u4e00-\u9fa5])((?:{COMPOUND_SURNAMES}|[{SURNAMES}])[\u4e00-\u9fa5]{{1,2}})"
    r"(?=\s*(?:[/／、,，;；|]|$))")
PERSON_STOP = {
    "项目部", "经理部", "工程部", "财务部", "技术部", "安全部", "质量部",
    "负责人", "联系人", "经办人", "招标人", "投标人", "中标人", "分包人",
    "监理人", "代理人", "委托人", "甲方", "乙方", "本单位", "公司", "单位",
    "本工程", "项目部经理", "项目经理", "技术负责人", "法定代表人", "法人代表",
    "总工程师", "工程师", "主任", "经理", "先生", "女士", "同志", "中标",
}
PERSON_BAD_SUFFIX = (
    "公司", "集团", "经理", "主任", "工程", "项目", "单位", "部门", "中心",
    "负责", "管理", "技术", "施工", "安全", "质量", "先生", "女士", "同志", "工", "总",
)


def _smart_available() -> bool:
    try:
        import ner_backend
        return ner_backend.available()
    except Exception:
        import importlib.util
        return importlib.util.find_spec("jieba") is not None


def smart_available() -> bool:
    return _smart_available()


def ner_info() -> dict:
    """当前 NER 后端信息：onnx（内置模型）/ jieba（兜底）/ none"""
    try:
        import ner_backend
        return ner_backend.info()
    except Exception:
        return {"active": "none", "available": [], "model": ""}


def ner_hits(text: str, tok=None, ner=None) -> list[Hit]:
    """可插拔 NER 接口。

    tok / ner 参数仅为兼容旧版 HanLP 调用方式，当前由 ner_backend 自动选择后端：
    内置 ONNX 模型优先，缺失时自动降级 jieba 词性标注。
    """
    _ = (tok, ner)
    try:
        import ner_backend
        raw = ner_backend.detect(text)
    except Exception:
        raw = []
    hits: list[Hit] = []
    for item in raw:
        category = str(item.get("category") or item.get("label") or "")
        start = int(item.get("start", -1))
        end = int(item.get("end", -1))
        if category not in {"PERSON", "COMPANY", "ADDRESS"} or not 0 <= start < end <= len(text):
            continue
        value = text[start:end]
        if category == "PERSON" and not _person_ok(value, require_surname=True):
            continue
        if category == "COMPANY" and not _company_ok(value):
            continue
        if category == "ADDRESS" and not _address_ok(value):
            continue
        hits.append(Hit(start, end, category, value, "智能"))
    return hits


def _person_ok(name: str, require_surname: bool = False) -> bool:
    name = name.strip("·")
    if not 2 <= len(name) <= 4 or not re.fullmatch(r"[\u4e00-\u9fa5·]+", name):
        return False
    if name in PERSON_STOP:
        return False
    if any(name.endswith(x) for x in PERSON_BAD_SUFFIX):
        return False
    if require_surname:
        if not (name.startswith(tuple(COMPOUND_SURNAMES.split("|"))) or name[0] in SURNAMES):
            return False
    return True


def _company_ok(name: str) -> bool:
    if not 4 <= len(name) <= 40:
        return False
    if name in COMPANY_GENERIC:
        return False
    suffix = next((s for s in COMPANY_SUFFIXES if name.endswith(s)), None)
    if not suffix:
        return False
    if suffix in {"中心", "厂", "店"} and len(name) < 6:
        return False
    return True


def _trim_noise(value: str, prefixes: Iterable[str]) -> str:
    value = value.strip(" \t\r\n，。；：、,.;:!?！？（）()《》【】[]{}<>“”\"'‘’")
    prefixes = sorted(prefixes, key=len, reverse=True)
    changed = True
    while changed and value:
        changed = False
        for p in prefixes:
            if value.startswith(p) and len(value) > len(p):
                value = value[len(p):].lstrip("：:，,。；; ")
                changed = True
                break
    return value


def _expand_company_left(text: str, suffix_start: int) -> int:
    i = suffix_start
    limit = max(0, suffix_start - 40)
    while i > limit:
        ch = text[i - 1]
        if ch in COMPANY_BOUNDARY:
            break
        if "\u4e00" <= ch <= "\u9fa5" or ch.isalnum() or ch in COMPANY_NAME_CHARS:
            i -= 1
            continue
        break
    return i


def find_companies(text: str) -> list[Hit]:
    candidates: dict[tuple[int, int], Hit] = {}
    for m in COMPANY_SUFFIX_RE.finditer(text):
        start = _expand_company_left(text, m.start())
        end = m.end()
        name = _trim_noise(text[start:end], COMPANY_PREFIX_NOISE)
        if not _company_ok(name):
            continue
        # 修剪前缀后重新定位（用 rfind 从原片段左侧兜底）
        adjusted_start = end - len(name)
        if adjusted_start < 0:
            adjusted_start = start
        candidates[(adjusted_start, end)] = Hit(adjusted_start, end, "COMPANY", name, "规则")
    # 长候选优先：避免「北京城建集团」把「北京城建集团有限责任公司」截断
    hits = sorted(candidates.values(), key=lambda h: h.end - h.start, reverse=True)
    chosen: list[Hit] = []
    for hit in hits:
        if any(hit.start < c.end and hit.end > c.start for c in chosen):
            continue
        chosen.append(hit)
    return sorted(chosen, key=lambda h: h.start)


def _address_ok(value: str) -> bool:
    if not 6 <= len(value) <= 60:
        return False
    if not any(x in value for x in "省市区县镇乡街路巷弄号栋楼室层"):
        return False
    return True


def find_addresses(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for m in ADDRESS_RE.finditer(text):
        raw = m.group()
        value = _trim_noise(raw, [
            "注册地址为", "办公地址为", "联系地址为", "通讯地址为",
            "注册地址", "办公地址", "联系地址", "通讯地址",
            "办公地点位于", "办公地点", "所在地为", "所在地",
            "地址为", "地址", "住址为", "住址", "住所为", "住所",
            "本工程位于", "本项目位于", "位于", "在", "为", "是", "：", ":",
        ])
        if not _address_ok(value):
            continue
        start = m.end() - len(value)
        if start < 0:
            start = m.start()
        hits.append(Hit(start, m.end(), "ADDRESS", value, "规则"))
    for m in ADDRESS_LABEL_RE.finditer(text):
        value = m.group(1)
        if not _address_ok(value):
            continue
        if not any(x in value for x in "省市区县街路巷弄号楼栋室层"):
            continue
        hits.append(Hit(m.start(1), m.end(1), "ADDRESS", value, "规则"))
    hits.sort(key=lambda h: (h.end - h.start), reverse=True)
    chosen: list[Hit] = []
    for hit in hits:
        if any(hit.start < c.end and hit.end > c.start for c in chosen):
            continue
        chosen.append(hit)
    return sorted(chosen, key=lambda h: h.start)


def _whitelist_spans(text: str, whitelist: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for word in whitelist:
        start = 0
        while word:
            idx = text.find(word, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(word)))
            start = idx + len(word)
    return spans


def _near_label(text: str, pos: int, labels: tuple[str, ...], width: int = 10) -> bool:
    left = text[max(0, pos - width):pos]
    return any(lbl in left for lbl in labels)


def detect(
    text: str,
    vocab: list[tuple[str, str]] | None = None,
    whitelist: list[str] | None = None,
    use_smart: bool = True,
) -> list[Hit]:
    vocab = vocab or []
    whitelist = whitelist or []
    hits: list[Hit] = []
    used: list[tuple[int, int]] = []
    wl_spans = _whitelist_spans(text, whitelist)
    company_hits = find_companies(text)
    # 白名单命中公司全称的一部分时，整段公司名都保留
    # 例如白名单「中国银行」→「中国银行北京分行营业部」整体不脱
    for company in company_hits:
        if any(w and w in company.text for w in whitelist):
            wl_spans.append((company.start, company.end))

    def overlaps(spans: list[tuple[int, int]], s: int, e: int) -> bool:
        return any(s < ue and e > us for us, ue in spans)

    def add(s: int, e: int, cat: str, src: str, value: Optional[str] = None) -> bool:
        if s < 0 or e > len(text) or s >= e:
            return False
        if overlaps(wl_spans, s, e) or overlaps(used, s, e):
            return False
        value = text[s:e] if value is None else value
        hits.append(Hit(s, e, cat, value, src))
        used.append((s, e))
        return True

    # 1) 词库 / 补充敏感词（最高优先级）
    for word, cat in vocab:
        if not word:
            continue
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            if overlaps(wl_spans, idx, idx + len(word)) or overlaps(used, idx, idx + len(word)):
                start = idx + 1
                continue
            add(idx, idx + len(word), cat, "词库")
            start = idx + len(word)

    # 2) 正则硬规则
    for m in EMAIL_RE.finditer(text):
        add(m.start(), m.end(), "EMAIL", "规则")

    for m in IDCARD_RE.finditer(text):
        value = m.group(1)
        if idcard_ok(value) or _near_label(text, m.start(1), ("身份证", "证件号", "证件号码")):
            add(m.start(1), m.end(1), "IDCARD", "规则")

    for m in MOBILE_RE.finditer(text):
        add(m.start(), m.end(), "PHONE", "规则")
    for m in LANDLINE_RE.finditer(text):
        add(m.start(), m.end(), "PHONE", "规则")

    for m in BANK_CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if luhn_ok(digits):
            add(m.start(), m.end(), "BANK", "规则")
    for m in BANK_LABEL_RE.finditer(text):
        end = m.end(1)
        while end > m.start(1) and text[end - 1] in " \t-－":
            end -= 1
        digits = re.sub(r"\D", "", text[m.start(1):end])
        if 10 <= len(digits) <= 24 and not overlaps(used, m.start(1), end):
            add(m.start(1), end, "BANK", "规则")

    for m in USCC_RE.finditer(text):
        if uscc_ok(m.group()):
            add(m.start(), m.end(), "CREDITCODE", "规则")

    for m in CN_AMOUNT_RE.finditer(text):
        add(m.start(), m.end(), "AMOUNT", "规则")

    for m in CURRENCY_AMOUNT_RE.finditer(text):
        add(m.start(), m.end(), "AMOUNT", "规则")

    for m in AMOUNT_LABEL_RE.finditer(text):
        end = m.end(2) if m.group(2) else m.end(1)
        add(m.start(1), end, "AMOUNT", "规则")

    for m in CODE_LABEL_RE.finditer(text):
        value = m.group(1)
        if value not in PROJECT_GENERIC:
            add(m.start(1), m.end(1), "CODE", "规则")
    for m in PROJECT_NAME_RE.finditer(text):
        value = m.group(1)
        if value not in PROJECT_GENERIC and len(value) >= 4:
            add(m.start(1), m.end(1), "CODE", "规则")
    for m in ALNUM_CODE_RE.finditer(text):
        add(m.start(), m.end(), "CODE", "规则")

    for hit in find_addresses(text):
        add(hit.start, hit.end, "ADDRESS", "规则", hit.text)

    for hit in find_companies(text):
        add(hit.start, hit.end, "COMPANY", "规则", hit.text)

    for m in PERSON_ROLE_RE.finditer(text):
        name = m.group(1)
        if _person_ok(name):
            add(m.start(1), m.end(1), "PERSON", "规则")
    for m in NAME_STANDALONE_RE.finditer(text):
        name = m.group(1)
        if _person_ok(name, require_surname=True):
            add(m.start(1), m.end(1), "PERSON", "规则")

    # 3) 本地智能：可插拔 NER（内置 ONNX 优先，jieba 兜底）
    if use_smart:
        for hit in ner_hits(text):
            add(hit.start, hit.end, hit.category, "智能", hit.text)

    hits.sort(key=lambda h: h.start)
    return hits


# =====================================================================
# 五、文档遍历 / run 级替换
# =====================================================================
def iter_paragraphs(doc) -> Iterable[Paragraph]:
    """正文、表格（含嵌套）、文本框、页眉页脚、脚注/尾注/批注里的段落"""
    # 说明：不要用 id(p) 去重。lxml 的节点代理对象可能被回收并复用内存地址，
    # 用 id() 记已访问会导致部分段落被误跳过（表格、正文后半段最容易中招）。
    for p in doc.element.body.iter(qn("w:p")):
        yield Paragraph(p, doc)

    seen_content_parts: set[str] = set()
    content_parts = ("comments.xml", "footnotes.xml", "endnotes.xml")
    for part in doc.part.package.iter_parts():
        try:
            part_name = str(part.partname)
        except Exception:
            continue
        if part_name in seen_content_parts:
            continue
        if not any(part_name.endswith(name) for name in content_parts):
            continue
        seen_content_parts.add(part_name)
        element = getattr(part, "element", None)
        if element is None:
            continue
        for p in element.iter(qn("w:p")):
            yield Paragraph(p, doc)

    seen_parts: set[str] = set()
    for section in doc.sections:
        for hf in (
            section.header, section.footer,
            section.first_page_header, section.first_page_footer,
            section.even_page_header, section.even_page_footer,
        ):
            try:
                if hf.is_linked_to_previous:
                    continue
                part_name = str(hf.part.partname)
                if part_name in seen_parts:
                    continue
                seen_parts.add(part_name)
                for p in hf._element.iter(qn("w:p")):
                    yield Paragraph(p, doc)
            except Exception:
                continue


def iter_runs(para: Paragraph) -> list[Run]:
    """包含超链接、智能标记等嵌套结构里的 run"""
    return [Run(r, para) for r in para._p.iter(qn("w:r"))]


def paragraph_text(para: Paragraph) -> str:
    return "".join(r.text for r in iter_runs(para))


def _replace_spans(para: Paragraph, plan: list[tuple[list[tuple[int, int, int]], str]]):
    """按 run 级区间替换，从右往左执行，保留字体格式"""
    runs = iter_runs(para)
    if not runs:
        return
    runs_text = [r.text for r in runs]
    for spans, repl in reversed(plan):
        first_ri, fs, _ = spans[0]
        last_ri, _, le = spans[-1]
        if first_ri == last_ri:
            t = runs_text[first_ri]
            runs_text[first_ri] = t[:fs] + repl + t[le:]
        else:
            runs_text[first_ri] = runs_text[first_ri][:fs] + repl
            for ri, _fs, _fe in spans[1:-1]:
                runs_text[ri] = ""
            runs_text[last_ri] = runs_text[last_ri][le:]
    for i, r in enumerate(runs):
        if r.text != runs_text[i]:
            r.text = runs_text[i]


def _spans_for(hits: list[Hit], runs_text: list[str]) -> list[tuple[list[tuple[int, int, int]], str]]:
    plan: list[tuple[list[tuple[int, int, int]], str]] = []
    total = sum(len(t) for t in runs_text)
    for hit in hits:
        s, e = hit.start, hit.end
        if s < 0 or e > total:
            continue
        pos, spans = 0, []
        for ri, t in enumerate(runs_text):
            rl = len(t)
            if pos + rl > s and pos < e:
                spans.append((ri, max(s - pos, 0), min(e - pos, rl)))
            pos += rl
        if spans:
            plan.append((spans, hit))
    return plan


# =====================================================================
# 六、替换工厂（占位符 / 拟真 / 星号 / 删除）
# =====================================================================
COMPANY_FAKE = [
    "启元建设工程有限公司", "恒泰工程集团有限公司", "华筑建设发展有限公司",
    "中远市政工程有限公司", "博远建筑工程有限公司", "嘉禾建设集团有限公司",
    "瑞成工程有限责任公司", "天工建设有限公司", "新程交通建设有限公司",
    "正弘工程有限公司", "联创建设工程有限公司", "安泰建设集团有限公司",
    "弘毅建筑工程有限公司", "远景市政建设有限公司", "同和建设发展有限公司",
    "恒信工程有限公司", "兴华建设集团有限公司", "中元建设工程有限公司",
    "德胜建筑工程有限公司", "开元建设有限公司",
]
PERSON_FAKE = [
    "王明", "李华", "张强", "刘洋", "陈静", "杨帆", "赵磊", "黄敏",
    "周涛", "吴婷", "徐亮", "孙悦", "马超", "朱琳", "胡军", "郭静",
    "何平", "高翔", "林峰", "罗丹", "郑凯", "梁雪", "谢峰", "宋佳",
    "韩磊", "唐宁", "许静", "邓超", "曹阳", "彭飞",
]
ADDRESS_FAKE = [
    "北京市朝阳区新元路{a}号", "上海市浦东新区云台路{a}号",
    "广州市天河区明德路{a}号", "深圳市南山区科苑路{a}号",
    "杭州市西湖区文新路{a}号", "成都市武侯区锦江路{a}号",
    "南京市建邺区江东中路{a}号", "武汉市洪山区珞喻路{a}号",
]
CN_DIGIT_MAP = {
    "壹": "叁", "贰": "柒", "叁": "伍", "肆": "玖", "伍": "贰",
    "陆": "捌", "柒": "壹", "捌": "肆", "玖": "陆", "零": "零",
}
AMOUNT_NUM_CHARS = set("0123456789" + CN_NUM)


def star_mask(text: str, cat: str) -> str:
    if cat == "PERSON":
        return text[0] + "**"
    if cat == "PHONE":
        return text[:3] + "****" + text[-4:]
    if cat == "IDCARD":
        return text[:6] + "********" + text[-4:]
    if cat == "BANK":
        return text[:4] + "*" * max(0, len(text) - 8) + text[-4:]
    if cat == "EMAIL":
        return text.partition("@")[0][:2] + "****"
    if cat == "AMOUNT":
        return "".join("*" if c in AMOUNT_NUM_CHARS else c for c in text)
    return text[:2] + "****"


def _permute_digits(text: str, seed: str) -> str:
    rng = random.Random(seed)
    out = []
    for i, ch in enumerate(text):
        if ch.isdigit():
            prev_digit = i > 0 and text[i - 1].isdigit()
            out.append(str(rng.randrange(10)) if prev_digit else ch)
        else:
            out.append(ch)
    return "".join(out)


def _permute_code(text: str, seed: str) -> str:
    rng = random.Random(seed)
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    out = []
    for ch in text:
        if ch.isalpha():
            c = rng.choice(letters)
            out.append(c.lower() if ch.islower() else c)
        elif ch.isdigit():
            out.append(str(rng.randrange(10)))
        else:
            out.append(ch)
    return "".join(out)


def _fake_idcard(seq: int) -> str:
    body = "11010119900101" + f"{seq % 1000:03d}"
    return body + idcard_check_digit(body)


def _fake_bank(seq: int) -> str:
    prefix = "622202" + f"{seq % 10_000_000_000:010d}"
    return prefix + luhn_check_digit(prefix)


def _fake_uscc(seq: int) -> str:
    body = "91110000" + f"{seq % 100_000_000:08d}" + "X"
    return body + uscc_check_digit(body)


class ReplacementFactory:
    def __init__(self, mode: str, strategies: dict[str, str], source_text: str):
        self.mode = mode if mode in MODE_CN else "placeholder"
        self.strategies = strategies or {}
        self.source_text = source_text
        self.mapping: dict[tuple[str, str], str] = {}
        self.items: list[dict] = []
        self._item_index: dict[tuple[str, str, str], dict] = {}
        self.counters: dict[str, int] = defaultdict(int)
        self.used: set[str] = set()

    def _strategy(self, cat: str) -> str:
        return self.strategies.get(cat, "fake")

    def _placeholder(self, cat: str) -> str:
        return f"【{CAT_CN[cat]}{tag_letter(self.counters[cat])}】"

    def _realistic(self, cat: str, text: str, attempt: int) -> str:
        n = self.counters[cat] + 1 + attempt
        seed = f"{cat}:{text}:{n}"
        if cat == "COMPANY":
            return COMPANY_FAKE[(n - 1) % len(COMPANY_FAKE)]
        if cat == "PERSON":
            return PERSON_FAKE[(n - 1) % len(PERSON_FAKE)]
        if cat == "PHONE":
            if text.startswith(("400", "800")):
                return text[:3] + "-000-" + f"{n % 10000:04d}"
            if text.startswith("0") or "-" in text or "－" in text:
                return "010-0000" + f"{n % 10000:04d}"
            return "1990000" + f"{n % 10000:04d}"
        if cat == "IDCARD":
            return _fake_idcard(n)
        if cat == "BANK":
            return _fake_bank(n)
        if cat == "EMAIL":
            return f"user{n % 10000:04d}@example.com"
        if cat == "ADDRESS":
            return ADDRESS_FAKE[(n - 1) % len(ADDRESS_FAKE)].format(a=n)
        if cat == "CREDITCODE":
            return _fake_uscc(n)
        if cat == "AMOUNT":
            if any(c in text for c in "零壹贰叁肆伍陆柒捌玖"):
                return "".join(CN_DIGIT_MAP.get(c, c) for c in text)
            return _permute_digits(text, seed)
        if cat == "CODE":
            if re.search(r"[\u4e00-\u9fa5]", text):
                return self._placeholder(cat)
            return _permute_code(text, seed)
        return f"【{CAT_CN.get(cat, '敏感')}{tag_letter(n - 1)}】"

    def get(self, cat: str, text: str) -> str:
        strat = self._strategy(cat)
        key = (cat, text, strat)
        item = self._item_index.get(key)
        if item is None:
            if strat == "star":
                repl = star_mask(text, cat)
                self.counters[cat] = self.counters.get(cat, 0) + 1
            elif strat == "delete":
                # 严格删除：不保留占位文本，也不进入还原映射
                self.counters[cat] = self.counters.get(cat, 0) + 1
                repl = ""
            else:
                repl = self._make_fake(cat, text)
            item = {
                "category": cat,
                "category_cn": CAT_CN.get(cat, cat),
                "original": text,
                "replacement": repl,
                "strategy": strat,
                "strategy_cn": STRATEGY_CN.get(strat, strat),
                "reversible": strat == "fake",
                "count": 0,
            }
            self.items.append(item)
            self._item_index[key] = item
        item["count"] += 1
        return item["replacement"]

    def _make_fake(self, cat: str, text: str) -> str:
        key = (cat, text)
        if key in self.mapping:
            return self.mapping[key]
        for attempt in range(0, 200):
            repl = self._placeholder(cat) if self.mode == "placeholder" else self._realistic(cat, text, attempt)
            if not repl:
                continue
            if repl in self.source_text or repl in self.used:
                continue
            self.mapping[key] = repl
            self.used.add(repl)
            self.counters[cat] += 1
            return repl
        # 极端兜底：加序号
        repl = f"【{CAT_CN.get(cat, '敏感')}{len(self.used)}】"
        self.mapping[key] = repl
        self.used.add(repl)
        self.counters[cat] += 1
        return repl


# =====================================================================
# 七、分析（供 Web 预览/复核）
# =====================================================================
@dataclass
class Analysis:
    findings: list[dict] = field(default_factory=list)
    occurrences: int = 0
    stats: dict = field(default_factory=dict)


def _context(text: str, start: int, end: int, width: int = 28) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    snippet = text[left:start] + "⟦" + text[start:end] + "⟧" + text[end:right]
    return re.sub(r"\s+", " ", snippet).strip()


def _aggregate(records: list[tuple[str, list[Hit]]]) -> Analysis:
    bucket: dict[str, dict] = {}
    total = 0
    for text, hits in records:
        for hit in hits:
            total += 1
            key = hit.key
            item = bucket.get(key)
            if item is None:
                item = {
                    "key": key,
                    "category": hit.category,
                    "category_cn": CAT_CN.get(hit.category, hit.category),
                    "text": hit.text,
                    "count": 0,
                    "contexts": [],
                    "occurrences": [],
                    "sources": [],
                }
                bucket[key] = item
            item["count"] += 1
            if hit.source not in item["sources"]:
                item["sources"].append(hit.source)
            occurrence_id = getattr(hit, "occurrence_key", "")
            if occurrence_id:
                occurrence = {
                    "id": occurrence_id,
                    "context": _context(text, hit.start, hit.end),
                }
                item["occurrences"].append(occurrence)
            if len(item["contexts"]) < 3:
                ctx = _context(text, hit.start, hit.end)
                if ctx and ctx not in item["contexts"]:
                    item["contexts"].append(ctx)
    findings = list(bucket.values())
    findings.sort(key=lambda f: (CAT_ORDER.index(f["category"]) if f["category"] in CAT_ORDER else 99,
                                 -f["count"], f["text"]))
    for f in findings:
        f["source"] = "+".join(f.pop("sources")) or "规则"
    return Analysis(findings=findings, occurrences=total)


def _collect_docx(doc, vocab, whitelist, use_smart) -> tuple[list[tuple[Paragraph, str, list[Hit]]], Analysis]:
    records: list[tuple[Paragraph, str, list[Hit]]] = []
    agg: list[tuple[str, list[Hit]]] = []
    for record_index, para in enumerate(iter_paragraphs(doc)):
        text = paragraph_text(para)
        if not text.strip():
            records.append((para, text, []))
            continue
        hits = _assign_occurrence_keys(
            detect(text, vocab, whitelist, use_smart=use_smart),
            text,
            record_index,
        )
        records.append((para, text, hits))
        agg.append((text, hits))
    return records, _aggregate(agg)


def _collect_text(text: str, vocab, whitelist, use_smart):
    lines = text.splitlines(keepends=True)
    records = []
    agg = []
    for record_index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        hits = _assign_occurrence_keys(
            detect(body, vocab, whitelist, use_smart=use_smart) if body.strip() else [],
            body,
            record_index,
        )
        records.append((body, hits))
        agg.append((body, hits))
    return records, _aggregate(agg)


def analyze_document(
    src: Path,
    *,
    vocab: list[tuple[str, str]] | None = None,
    whitelist: list[str] | None = None,
    custom_words: str = "",
    use_smart: bool = True,
) -> Analysis:
    extra_vocab, extra_wl = parse_word_spec(custom_words)
    vocab_all = list(extra_vocab) + list(vocab or [])
    whitelist_all = list(whitelist or []) + extra_wl
    suffix = src.suffix.lower()
    if suffix == ".docx":
        doc = Document(str(src))
        _, analysis = _collect_docx(doc, vocab_all, whitelist_all, use_smart and smart_available())
        analysis.stats = {
            "文件类型": "Word (.docx)",
            "段落数": sum(1 for _ in iter_paragraphs(doc)),
            "表格数": len(doc.tables),
            "节数": len(doc.sections),
        }
        return analysis
    if suffix in {".txt", ".md", ".markdown"}:
        text = src.read_text(encoding="utf-8", errors="replace")
        _, analysis = _collect_text(text, vocab_all, whitelist_all, use_smart and smart_available())
        analysis.stats = {"文件类型": "文本", "行数": len(text.splitlines())}
        return analysis
    raise ValueError("暂不支持该文件格式，请上传 .docx / .txt / .md")


# =====================================================================
# 七点五、脱敏前后对比预览（不落盘）
# =====================================================================
def _preview_segments(
    text: str,
    hits: list[Hit],
    replacements: list[str],
    style_segments: Optional[list[dict]] = None,
):
    """把一段文字拆成左右两栏片段，changed=True 的片段为命中项"""
    orig_parts: list[dict] = []
    red_parts: list[dict] = []
    cursor = 0
    if not style_segments:
        style_segments = [{"text": text, "style": {}}]
    spans: list[tuple[int, int, dict]] = []
    position = 0
    for segment in style_segments:
        segment_text = str(segment.get("text", ""))
        spans.append((position, position + len(segment_text), segment.get("style") or {}))
        position += len(segment_text)
    if position < len(text):
        spans.append((position, len(text), {}))

    def style_at(offset: int) -> dict:
        for start, end, style in spans:
            if start <= offset < end:
                return dict(style)
        return {}

    def parts_in_range(start: int, end: int) -> list[dict]:
        parts: list[dict] = []
        for seg_start, seg_end, style in spans:
            overlap_start = max(start, seg_start)
            overlap_end = min(end, seg_end)
            if overlap_start >= overlap_end:
                continue
            parts.append({
                "text": text[overlap_start:overlap_end],
                "changed": False,
                "style": dict(style),
            })
        return parts

    for hit, repl in zip(hits, replacements):
        if hit.start > cursor:
            for part in parts_in_range(cursor, hit.start):
                orig_parts.append(part)
                red_parts.append(dict(part))
        style = style_at(min(hit.start, max(len(text) - 1, 0)))
        meta = {
            "changed": True,
            "category": hit.category,
            "category_cn": CAT_CN.get(hit.category, hit.category),
        }
        orig_parts.append({"text": text[hit.start:hit.end], "style": style, **meta})
        red_parts.append({"text": repl, "style": style, **meta})
        cursor = hit.end
    if cursor < len(text):
        for part in parts_in_range(cursor, len(text)):
            orig_parts.append(part)
            red_parts.append(dict(part))
    return orig_parts, red_parts


def _heading_level(para: Paragraph) -> int:
    try:
        name = (para.style.name or "").strip().lower()
    except Exception:
        name = ""
    m = re.search(r"(?:heading|标题)\s*([1-6])", name)
    if m:
        return int(m.group(1))
    if name in {"title", "标题"}:
        return 1
    try:
        p_pr = para._p.pPr
        if p_pr is not None:
            lvl = p_pr.find(qn("w:outlineLvl"))
            if lvl is not None:
                return min(max(int(lvl.get(qn("w:val"))) + 1, 1), 6)
    except Exception:
        pass
    return 0


def _css_pt(value) -> Optional[str]:
    try:
        if value is None:
            return None
        return f"{value.pt:.2f}pt".rstrip("0").rstrip(".")
    except Exception:
        return None


def _quote_font(name: str) -> str:
    name = str(name or "").strip()
    return f'"{name}"' if name and " " in name else name


def _style_chain_css(style) -> dict:
    css: dict = {}
    seen: set[str] = set()
    while style is not None:
        key = str(getattr(style, "style_id", "") or getattr(style, "name", ""))
        if not key or key in seen:
            break
        seen.add(key)
        font = getattr(style, "font", None)
        pf = getattr(style, "paragraph_format", None)
        if font is not None:
            name = _quote_font(font.name)
            size = _css_pt(font.size)
            if name:
                css.setdefault("font-family", name)
            if size:
                css.setdefault("font-size", size)
            if font.bold:
                css.setdefault("font-weight", "700")
            if font.italic:
                css.setdefault("font-style", "italic")
            if font.underline:
                css.setdefault("text-decoration", "underline")
            if font.strike:
                css.setdefault("text-decoration-line", "line-through")
            try:
                if font.color is not None and font.color.rgb is not None:
                    css.setdefault("color", str(font.color.rgb))
            except Exception:
                pass
        if pf is not None:
            alignment = getattr(pf.alignment, "name", None)
            alignment_map = {
                "CENTER": "center", "RIGHT": "right", "JUSTIFY": "justify",
                "DISTRIBUTE": "justify", "JUSTIFY_LOW": "justify",
            }
            if alignment in alignment_map:
                css.setdefault("text-align", alignment_map[alignment])
            for attr, prop in (
                ("left_indent", "margin-left"),
                ("right_indent", "margin-right"),
                ("first_line_indent", "text-indent"),
                ("space_before", "margin-top"),
                ("space_after", "margin-bottom"),
            ):
                value = _css_pt(getattr(pf, attr, None))
                if value:
                    css.setdefault(prop, value)
            if pf.line_spacing:
                css.setdefault("line-height", str(pf.line_spacing))
        style = getattr(style, "base_style", None)
    return css


def _paragraph_style(para: Paragraph) -> dict:
    css = _style_chain_css(getattr(para, "style", None))
    pf = para.paragraph_format
    alignment = getattr(pf.alignment, "name", None)
    alignment_map = {
        "CENTER": "center", "RIGHT": "right", "JUSTIFY": "justify",
        "DISTRIBUTE": "justify", "JUSTIFY_LOW": "justify",
    }
    if alignment in alignment_map:
        css["text-align"] = alignment_map[alignment]
    for attr, prop in (
        ("left_indent", "margin-left"),
        ("right_indent", "margin-right"),
        ("first_line_indent", "text-indent"),
        ("space_before", "margin-top"),
        ("space_after", "margin-bottom"),
    ):
        value = _css_pt(getattr(pf, attr, None))
        if value:
            css[prop] = value
    if pf.line_spacing:
        css["line-height"] = str(pf.line_spacing)
    return css


def _run_style(run: Run, para: Paragraph) -> dict:
    css = _style_chain_css(getattr(para, "style", None))
    font = run.font
    name = _quote_font(font.name)
    size = _css_pt(font.size)
    if name:
        css["font-family"] = name
    if size:
        css["font-size"] = size
    if font.bold is True:
        css["font-weight"] = "700"
    elif font.bold is False:
        css["font-weight"] = "400"
    if font.italic is True:
        css["font-style"] = "italic"
    elif font.italic is False:
        css.pop("font-style", None)
    decorations = []
    if font.underline:
        decorations.append("underline")
    if font.strike:
        decorations.append("line-through")
    if decorations:
        css["text-decoration"] = " ".join(decorations)
    try:
        if font.color is not None and font.color.rgb is not None:
            css["color"] = str(font.color.rgb)
    except Exception:
        pass
    if font.highlight_color is not None:
        css["background-color"] = str(font.highlight_color).split(".")[-1].lower()
    if font.subscript:
        css["vertical-align"] = "sub"
    elif font.superscript:
        css["vertical-align"] = "super"
    return css


def _run_segments(para: Paragraph) -> list[dict]:
    segments: list[dict] = []
    for run in iter_runs(para):
        if run.text:
            segments.append({"text": run.text, "style": _run_style(run, para)})
    return segments


def _cell_style(cell) -> dict:
    first_para = None
    for p_el in cell._tc.iter(qn("w:p")):
        first_para = Paragraph(p_el, cell)
        break
    css = _paragraph_style(first_para) if first_para is not None else {}
    try:
        vertical = getattr(cell.vertical_alignment, "name", None)
        if vertical:
            css["vertical-align"] = vertical.lower()
    except Exception:
        pass
    try:
        tc_pr = cell._tc.tcPr
        shading = tc_pr.find(qn("w:shd")) if tc_pr is not None else None
        fill = shading.get(qn("w:fill")) if shading is not None else None
        if fill and fill.lower() not in {"auto", "ffffff"}:
            css["background-color"] = f"#{fill}"
    except Exception:
        pass
    return css


def _cell_preview_text(cell) -> str:
    lines = []
    for p_el in cell._tc.iter(qn("w:p")):
        text = paragraph_text(Paragraph(p_el, cell))
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


def _cell_paragraph_offsets(
    cell, occurrence_indices: dict[str, int]
) -> tuple[list[tuple[int, str, int]], str, list[dict]]:
    """返回单元格内每个有效段落的偏移、文本和规范记录序号。"""
    lines: list[str] = []
    entries: list[tuple[int, str, int]] = []
    segments: list[dict] = []
    offset = 0
    for p_el in cell._tc.iter(qn("w:p")):
        paragraph = Paragraph(p_el, cell)
        text = paragraph_text(paragraph)
        if not text.strip():
            continue
        if lines:
            offset += 1
            segments.append({"text": "\n", "style": {}})
        record_index = occurrence_indices.get(_paragraph_location_key(paragraph), -1)
        entries.append((offset, text, record_index))
        lines.append(text)
        segments.extend(_run_segments(paragraph))
        offset += len(text)
    return entries, "\n".join(lines), segments


def _iter_hf_paragraphs(doc):
    """页眉页脚段落，附带位置标签"""
    seen_parts: set[str] = set()
    for section in doc.sections:
        for hf, label in (
            (section.header, "页眉"), (section.footer, "页脚"),
            (section.first_page_header, "首页页眉"),
            (section.first_page_footer, "首页页脚"),
            (section.even_page_header, "偶数页页眉"),
            (section.even_page_footer, "偶数页页脚"),
        ):
            try:
                if hf.is_linked_to_previous:
                    continue
                part_name = str(hf.part.partname)
                if part_name in seen_parts:
                    continue
                seen_parts.add(part_name)
                for p in hf._element.iter(qn("w:p")):
                    yield Paragraph(p, doc), label
            except Exception:
                continue


def _iter_content_paragraphs(doc):
    """脚注、尾注与批注段落，附带位置标签。"""
    seen_parts: set[str] = set()
    for part in doc.part.package.iter_parts():
        try:
            part_name = str(part.partname)
        except Exception:
            continue
        if part_name in seen_parts:
            continue
        if not any(part_name.endswith(name) for name in ("comments.xml", "footnotes.xml", "endnotes.xml")):
            continue
        seen_parts.add(part_name)
        element = getattr(part, "element", None)
        if element is None:
            continue
        label = "批注" if part_name.endswith("comments.xml") else (
            "尾注" if part_name.endswith("endnotes.xml") else "脚注")
        for p in element.iter(qn("w:p")):
            yield Paragraph(p, doc), label


def build_preview_document(
    src: Path,
    *,
    mode: str = "placeholder",
    strategies: dict[str, str] | None = None,
    excluded: Iterable[str] | None = None,
    vocab: list[tuple[str, str]] | None = None,
    whitelist: list[str] | None = None,
    custom_words: str = "",
    use_smart: bool = True,
) -> dict:
    """按当前策略生成原文/脱敏文对照，不写任何文件。"""
    src = Path(src)
    mode = mode if mode in MODE_CN else "placeholder"
    strategies = {parse_category(k): STRATEGY_ALIAS.get(v, v)
                  for k, v in (strategies or {}).items()}
    strategies = {k: v for k, v in strategies.items() if v in STRATEGY_CN}
    excluded = set(excluded or [])

    extra_vocab, extra_wl = parse_word_spec(custom_words)
    vocab_all = list(extra_vocab) + list(vocab or [])
    whitelist_all = list(whitelist or []) + extra_wl
    use_smart = bool(use_smart and smart_available())
    suffix = src.suffix.lower()

    occurrences = 0

    def make_block(factory: ReplacementFactory, text: str,
                   block_type: str = "paragraph", level: int = 0,
                   label: str = "", record_index: Optional[int] = None,
                   paragraph_entries: Optional[list[tuple[int, str, int]]] = None,
                   style_segments: Optional[list[dict]] = None,
                   block_style: Optional[dict] = None) -> dict:
        nonlocal occurrences
        hits = detect(text, vocab_all, whitelist_all, use_smart=use_smart)
        if record_index is not None:
            _assign_occurrence_keys(hits, text, record_index)
        elif paragraph_entries:
            for hit in hits:
                entry = next((item for item in paragraph_entries
                              if hit.start >= item[0] and hit.start < item[0] + len(item[1])),
                             paragraph_entries[0])
                relative_start = hit.start - entry[0]
                hit.occurrence_key = _occurrence_key(
                    hit.category, hit.text, entry[2], relative_start)
        hits = [h for h in hits
                if h.key not in excluded and h.occurrence_key not in excluded]
        meta = {"type": block_type, "level": level, "label": label}
        if block_style:
            meta["style"] = block_style
        if not hits:
            plain, _ = _preview_segments(text, [], [], style_segments)
            return {**meta, "original": text, "redacted": text, "changed": False,
                    "original_parts": plain, "redacted_parts": plain}
        replacements = [factory.get(h.category, h.text) for h in hits]
        orig_parts, red_parts = _preview_segments(text, hits, replacements, style_segments)
        occurrences += len(hits)
        return {
            **meta,
            "original": text,
            "redacted": "".join(p["text"] for p in red_parts),
            "changed": True,
            "original_parts": orig_parts,
            "redacted_parts": red_parts,
        }

    def make_table_block(factory: ReplacementFactory, table) -> dict:
        rows = []
        changed = False
        col_widths: list[float] = []
        first_row_cells = table.rows[0].cells
        for index, cell in enumerate(first_row_cells):
            width = None
            if index < len(table.columns):
                width = _css_pt(table.columns[index].width)
            if not width:
                width = _css_pt(cell.width)
            if width:
                col_widths.append(float(width[:-2]))
        if len(col_widths) != len(first_row_cells):
            col_widths = []
        col_width_css: list[str] = []
        if col_widths:
            total = sum(col_widths)
            if total > 0:
                col_width_css = [f"{value / total * 100:.2f}%" for value in col_widths]
        for row in table.rows:
            cells = []
            for cell in row.cells:
                paragraph_entries, cell_text, cell_segments = _cell_paragraph_offsets(
                    cell, occurrence_indices)
                cell_block = make_block(
                    factory, cell_text, "cell", paragraph_entries=paragraph_entries,
                    style_segments=cell_segments, block_style=_cell_style(cell))
                if cell_block["changed"]:
                    changed = True
                cells.append({
                    "original": cell_block["original"],
                    "redacted": cell_block["redacted"],
                    "changed": cell_block["changed"],
                    "original_parts": cell_block["original_parts"],
                    "redacted_parts": cell_block["redacted_parts"],
                    "style": cell_block.get("style", {}),
                })
            rows.append(cells)
        return {"type": "table", "level": 0, "label": "", "changed": changed,
                "rows": rows, "col_widths": col_width_css}

    blocks: list[dict] = []
    if suffix == ".docx":
        doc = Document(str(src))
        occurrence_indices = _docx_occurrence_indices(doc)
        source_text = "\n".join(paragraph_text(p) for p in iter_paragraphs(doc))
        factory = ReplacementFactory(mode, strategies, source_text)
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                para = Paragraph(child, doc)
                text = paragraph_text(para)
                if text.strip():
                    level = _heading_level(para)
                    blocks.append(make_block(
                        factory, text, "heading" if level else "paragraph", level,
                        record_index=occurrence_indices.get(_paragraph_location_key(para)),
                        style_segments=_run_segments(para),
                        block_style=_paragraph_style(para)))
            elif child.tag == qn("w:tbl"):
                blocks.append(make_table_block(factory, Table(child, doc)))
        for para, label in _iter_content_paragraphs(doc):
            text = paragraph_text(para)
            if text.strip():
                blocks.append(make_block(
                    factory, text, "paragraph", 0, label,
                    record_index=occurrence_indices.get(_paragraph_location_key(para)),
                    style_segments=_run_segments(para),
                    block_style=_paragraph_style(para)))
        for para, label in _iter_hf_paragraphs(doc):
            text = paragraph_text(para)
            if text.strip():
                blocks.append(make_block(
                    factory, text, "paragraph", 0, label,
                    record_index=occurrence_indices.get(_paragraph_location_key(para)),
                    style_segments=_run_segments(para),
                    block_style=_paragraph_style(para)))
    elif suffix in {".txt", ".md", ".markdown"}:
        text = src.read_text(encoding="utf-8", errors="replace")
        factory = ReplacementFactory(mode, strategies, text)
        for record_index, line in enumerate(text.splitlines()):
            if line.strip():
                blocks.append(make_block(factory, line, record_index=record_index))
    else:
        raise ValueError("暂不支持该文件格式，请上传 .docx / .txt / .md")

    return {
        "blocks": blocks,
        "changed_blocks": sum(1 for b in blocks if b["changed"]),
        "entities": len(factory.items),
        "occurrences": occurrences,
    }


# =====================================================================
# 八、防漏扫描 + 加密映射
# =====================================================================
def _mask_for_report(value: str, cat: str) -> str:
    return star_mask(value, cat) if value else value


def leak_scan(
    text: str,
    whitelist: list[str],
    excluded_values: list[str],
    items: list[dict],
) -> list[str]:
    sanitized = text
    # 白名单、用户排除项以及生成的假值都不算泄漏（否则拟真电话/邮箱会误报）
    removable = list(whitelist) + list(excluded_values)
    removable += [i.get("replacement", "") for i in items if i.get("replacement")]
    for w in removable:
        if w:
            sanitized = sanitized.replace(w, " ")
    warnings: list[str] = []
    for item in items:
        if (item["original"] and item["original"] not in excluded_values
                and item["original"] in text):
            warnings.append(f"已识别原文疑似残留：{_mask_for_report(item['original'], item['category'])}")
    checks = [
        ("电话", MOBILE_RE), ("电话", LANDLINE_RE), ("邮箱", EMAIL_RE),
        ("身份证", IDCARD_RE), ("信用代码", USCC_RE),
        ("报价", CURRENCY_AMOUNT_RE), ("报价", CN_AMOUNT_RE),
    ]
    for label, pattern in checks:
        for m in pattern.finditer(sanitized):
            value = m.group()
            if label == "身份证" and not idcard_ok(value):
                continue
            if label == "信用代码" and not uscc_ok(value):
                continue
            warnings.append(f"疑似残留{label}：{value[:3]}***")
            break
    for m in BANK_CARD_RE.finditer(sanitized):
        if luhn_ok(re.sub(r"\D", "", m.group())):
            warnings.append(f"疑似残留银行卡号：{m.group()[:4]}***")
            break
    for m in re.finditer(r"\d{7,}", sanitized):
        value = m.group()
        if re.fullmatch(r"(19|20)\d{6}", value):
            continue
        warnings.append(f"存在 ≥7 位连续数字，请人工复核：{value[:3]}***")
        break
    if "@" in sanitized:
        warnings.append("文中残留 @ 符号，请核查是否为漏网邮箱")
    return warnings[:20]


KDF_ITERS = 200_000


def _derive_key(password: str, salt: bytes, iters: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=iters)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_payload(payload: dict, password: str) -> dict:
    salt = os.urandom(16)
    key = _derive_key(password, salt, KDF_ITERS)
    token = Fernet(key).encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
    return {
        "v": 2, "kdf": "PBKDF2-SHA256", "iters": KDF_ITERS,
        "salt": salt.hex(), "data": token,
    }


def decrypt_payload(container: dict, password: str) -> dict:
    key = _derive_key(password, bytes.fromhex(container["salt"]),
                      container.get("iters", KDF_ITERS))
    try:
        raw = Fernet(key).decrypt(container["data"].encode())
    except InvalidToken:
        raise ValueError("口令错误或映射文件损坏")
    return json.loads(raw.decode("utf-8"))


def load_mapping_data(path: Path, password: str | None = None) -> list[dict]:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(obj, dict) and "data" in obj:
        if not HAS_CRYPTO:
            raise ValueError("该映射已加密，请先安装 cryptography")
        if not password:
            raise ValueError("该映射文件已加密，请输入口令")
        obj = decrypt_payload(obj, password)
    if isinstance(obj, dict) and "items" in obj:
        return list(obj["items"])
    if isinstance(obj, dict):
        # 兼容旧版扁平映射 {原文: 占位符}
        return [
            {"category": "COMPANY", "original": k, "replacement": v,
             "strategy": "fake", "reversible": True}
            for k, v in obj.items()
        ]
    raise ValueError("映射文件格式无法识别")


# =====================================================================
# 九、主流程：脱敏
# =====================================================================
def _sanitize_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", stem).strip("._") or "文档"
    return stem[:80]


def _write_outputs(
    out_dir: Path,
    stem: str,
    suffix: str,
    payload: dict,
    *,
    report: dict,
    password: str | None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{stem}_脱敏{suffix}"
    if password:
        out_map = out_dir / f"{stem}_映射.enc"
        map_payload = encrypt_payload(payload, password)
    else:
        out_map = out_dir / f"{stem}_映射.json"
        map_payload = payload
    out_report = out_dir / f"{stem}_报告.json"
    out_map.write_text(json.dumps(map_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "redacted": out_file,
        "mapping": out_map,
        "report": out_report,
        "mapping_encrypted": bool(password),
    }


def _build_report(
    src: Path,
    mode: str,
    strategies: dict[str, str],
    items: list[dict],
    warnings: list[str],
    outputs: dict[str, Path],
    elapsed: float,
) -> dict:
    detail = []
    stats: dict[str, int] = {}
    for item in items:
        key = f"{item['category_cn']}({item['strategy_cn']})"
        stats[key] = stats.get(key, 0) + item["count"]
        original = _mask_for_report(item["original"], item["category"])
        detail.append({
            "类别": item["category_cn"],
            "原文": original,
            "替换为": item["replacement"],
            "策略": item["strategy_cn"],
            "可逆": item["reversible"],
            "出现次数": item["count"],
        })
    return {
        "工具": "标书脱敏工作台 v3",
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "源文件": src.name,
        "替换方式": MODE_CN.get(mode, mode),
        "策略配置": {CAT_CN.get(c, c): STRATEGY_CN.get(s, s) for c, s in strategies.items()},
        "命中统计": stats,
        "实体数": len(items),
        "防漏扫描": {"通过": not warnings, "警告": warnings},
        "耗时秒": round(elapsed, 2),
        "输出": {
            "脱敏文档": str(outputs["redacted"]),
            "映射文件": str(outputs["mapping"]),
        },
        "说明": "为避免二次泄漏，报告中的原文均按类别遮盖；完整原文仅保存在映射文件。",
        "明细": detail,
    }


def redact_document(
    src: Path,
    out_dir: Path,
    *,
    mode: str = "placeholder",
    strategies: dict[str, str] | None = None,
    excluded: Iterable[str] | None = None,
    password: str | None = None,
    vocab: list[tuple[str, str]] | None = None,
    whitelist: list[str] | None = None,
    custom_words: str = "",
    use_smart: bool = True,
) -> dict:
    t0 = time.time()
    src = Path(src)
    out_dir = Path(out_dir)
    strategies = {parse_category(k): v for k, v in (strategies or {}).items()}
    strategies = {k: STRATEGY_ALIAS.get(v, v) for k, v in strategies.items()}
    strategies = {k: v for k, v in strategies.items() if v in STRATEGY_CN}
    excluded = set(excluded or [])

    extra_vocab, extra_wl = parse_word_spec(custom_words)
    vocab_all = list(extra_vocab) + list(vocab or [])
    whitelist_all = list(whitelist or []) + extra_wl
    use_smart = bool(use_smart and smart_available())

    suffix = src.suffix.lower()
    stem = _sanitize_stem(src.name)
    processed_occurrences = 0

    def _scrub_docx_metadata(document) -> None:
        props = document.core_properties
        props.author = "已脱敏"
        props.last_modified_by = "已脱敏"
        props.title = ""
        props.subject = ""
        props.keywords = ""
        props.comments = ""
        props.category = ""
        props.content_status = ""
        props.identifier = ""
        props.language = ""
        props.version = ""
        props.revision = 1
        last_printed = props._element.find(qn("cp:lastPrinted"))
        if last_printed is not None:
            props._element.remove(last_printed)

    if suffix == ".docx":
        doc = Document(str(src))
        records, analysis = _collect_docx(doc, vocab_all, whitelist_all, use_smart)
        source_text = "\n".join(text for _p, text, _h in records)
        factory = ReplacementFactory(mode, strategies, source_text)
        excluded_values: list[str] = []
        for para, text, hits in records:
            active = []
            for hit in hits:
                if hit.key in excluded or hit.occurrence_key in excluded:
                    if hit.text not in excluded_values:
                        excluded_values.append(hit.text)
                    continue
                active.append(hit)
            if not active:
                continue
            processed_occurrences += len(active)
            runs_text = [r.text for r in iter_runs(para)]
            plan_hits = []
            for hit in active:
                repl = factory.get(hit.category, hit.text)
                plan_hits.append(Hit(hit.start, hit.end, hit.category, hit.text, hit.source))
                # 用工厂结果覆盖替换文本
                setattr(plan_hits[-1], "_repl", repl)
            plan = []
            for hit in plan_hits:
                spans_plan = _spans_for([hit], runs_text)
                if spans_plan:
                    plan.append((spans_plan[0][0], getattr(hit, "_repl")))
            _replace_spans(para, plan)
        out_file = out_dir / f"{stem}_脱敏.docx"
        out_dir.mkdir(parents=True, exist_ok=True)
        _scrub_docx_metadata(doc)
        doc.save(str(out_file))
        redacted_text = "\n".join(paragraph_text(p) for p in iter_paragraphs(doc))
    elif suffix in {".txt", ".md", ".markdown"}:
        text = src.read_text(encoding="utf-8", errors="replace")
        records, analysis = _collect_text(text, vocab_all, whitelist_all, use_smart)
        factory = ReplacementFactory(mode, strategies, text)
        out_lines = []
        excluded_values = []
        for body, hits in records:
            active = []
            for hit in hits:
                if hit.key in excluded or hit.occurrence_key in excluded:
                    if hit.text not in excluded_values:
                        excluded_values.append(hit.text)
                    continue
                active.append(hit)
            if not active:
                out_lines.append(body)
                continue
            processed_occurrences += len(active)
            new_line = body
            for hit in sorted(active, key=lambda h: h.start, reverse=True):
                repl = factory.get(hit.category, hit.text)
                new_line = new_line[:hit.start] + repl + new_line[hit.end:]
            out_lines.append(new_line)
        redacted_text = "\n".join(out_lines)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{stem}_脱敏{suffix}"
        out_file.write_text(redacted_text, encoding="utf-8")
    else:
        raise ValueError("暂不支持该文件格式，请上传 .docx / .txt / .md")

    items = factory.items
    warnings = leak_scan(redacted_text, whitelist_all, excluded_values, items)
    payload = {
        "version": 2,
        "tool": "标书脱敏工作台 v3",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": src.name,
        "mode": mode,
        "items": [i for i in items if i["reversible"]],
    }
    report = _build_report(src, mode, strategies, items, warnings,
                           {"redacted": out_file, "mapping": out_dir}, time.time() - t0)
    outputs = _write_outputs(out_dir, stem, suffix, payload,
                             report=report, password=password)
    report["输出"] = {"脱敏文档": str(outputs["redacted"]), "映射文件": str(outputs["mapping"])}
    outputs["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = {}
    for item in items:
        key = f"{item['category_cn']}({item['strategy_cn']})"
        stats[key] = stats.get(key, 0) + item["count"]
    return {
        "outputs": outputs,
        "items": items,
        "stats": stats,
        "warnings": warnings,
        "occurrences": processed_occurrences,
        "excluded_values": excluded_values,
        "elapsed": round(time.time() - t0, 2),
        "mapping_encrypted": bool(password),
    }


# =====================================================================
# 十、还原
# =====================================================================
def _build_reverse(items: list[dict]) -> dict[str, str]:
    reverse: dict[str, str] = {}
    for item in items:
        if not item.get("reversible", True):
            continue
        repl = item.get("replacement") or ""
        orig = item.get("original") or ""
        if repl and orig and repl not in reverse:
            reverse[repl] = orig
    return reverse


def _replace_literals(text: str, reverse: dict[str, str]) -> tuple[str, int]:
    keys = sorted(reverse, key=len, reverse=True)
    used: list[tuple[int, int]] = []
    hits: list[tuple[int, int, str]] = []
    for key in keys:
        start = 0
        while True:
            idx = text.find(key, start)
            if idx < 0:
                break
            end = idx + len(key)
            if not any(idx < ue and end > us for us, ue in used):
                used.append((idx, end))
                hits.append((idx, end, reverse[key]))
            start = idx + len(key)
    hits.sort(key=lambda x: x[0])
    out = text
    for s, e, orig in reversed(hits):
        out = out[:s] + orig + out[e:]
    return out, len(hits)


def restore_document(
    src: Path,
    mapping_path: Path,
    out_path: Path,
    password: str | None = None,
) -> dict:
    src = Path(src)
    out_path = Path(out_path)
    items = load_mapping_data(Path(mapping_path), password)
    reverse = _build_reverse(items)
    if not reverse:
        raise ValueError("映射为空，无法还原；请确认映射文件与脱敏文档匹配")
    count = 0
    suffix = src.suffix.lower()
    if suffix == ".docx":
        doc = Document(str(src))
        for para in iter_paragraphs(doc):
            text = paragraph_text(para)
            if not text:
                continue
            hits = []
            used: list[tuple[int, int]] = []
            for key in sorted(reverse, key=len, reverse=True):
                start = 0
                while True:
                    idx = text.find(key, start)
                    if idx < 0:
                        break
                    end = idx + len(key)
                    if not any(idx < ue and end > us for us, ue in used):
                        used.append((idx, end))
                        hits.append(Hit(idx, end, "COMPANY", key))
                    start = idx + len(key)
            if not hits:
                continue
            runs_text = [r.text for r in iter_runs(para)]
            plan = []
            for hit in sorted(hits, key=lambda h: h.start):
                spans_plan = _spans_for([hit], runs_text)
                if spans_plan:
                    plan.append((spans_plan[0][0], reverse[hit.text]))
            _replace_spans(para, plan)
            count += len(plan)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path))
    elif suffix in {".txt", ".md", ".markdown"}:
        text = src.read_text(encoding="utf-8", errors="replace")
        new_text, count = _replace_literals(text, reverse)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(new_text, encoding="utf-8")
    else:
        raise ValueError("暂不支持该文件格式，请上传 .docx / .txt / .md")
    return {"replaced": count, "output": str(out_path)}


# =====================================================================
# 十一、命令行
# =====================================================================
def _parse_strategies(items: list[str]) -> dict[str, str]:
    strategies = {}
    for item in items:
        cat, _, strat = item.partition("=")
        cat_key = parse_category(cat.strip(), "")
        strat_key = STRATEGY_ALIAS.get(strat.strip(), strat.strip())
        if not cat_key or strat_key not in STRATEGY_CN:
            raise SystemExit(f"无法理解策略配置: {item!r}（格式: 类别=假名/星号/删除）")
        strategies[cat_key] = strat_key
    return strategies


def main() -> None:
    ap = argparse.ArgumentParser(description="标书脱敏工具 v3（纯本地）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("redact", help="识别并脱敏")
    p1.add_argument("docx", type=Path, help="输入 .docx / .txt / .md")
    p1.add_argument("--vocab", type=Path, default=None)
    p1.add_argument("--custom", action="append", default=[],
                    help="补充敏感词，格式 类别|词，可多次使用")
    p1.add_argument("--strategy", action="append", default=[],
                    help="类别=假名/星号/删除，可多次使用（默认全部假名）")
    p1.add_argument("--mode", default="placeholder",
                    choices=["placeholder", "realistic", "占位符", "拟真"],
                    help="假名替换方式：placeholder 占位符 / realistic 拟真")
    p1.add_argument("--password", default=None, help="映射加密口令")
    p1.add_argument("--dry-run", action="store_true", help="只识别不替换")
    p1.add_argument("--out-dir", type=Path, default=None)
    p1.add_argument("--no-smart", action="store_true", help="关闭本地词性智能识别")
    p1.add_argument("--ner", action="store_true", help="兼容旧参数：本地智能识别默认开启")

    p2 = sub.add_parser("restore", help="用映射表还原")
    p2.add_argument("docx", type=Path)
    p2.add_argument("--mapping", type=Path, required=True)
    p2.add_argument("--password", default=None)
    p2.add_argument("-o", "--out", type=Path, required=True)

    args = ap.parse_args()
    if args.cmd == "redact":
        vocab, whitelist = load_vocab(args.vocab)
        custom = "\n".join(args.custom)
        strategies = _parse_strategies(args.strategy)
        mode = {"占位符": "placeholder", "拟真": "realistic"}.get(args.mode, args.mode)
        if args.dry_run:
            analysis = analyze_document(args.docx, vocab=vocab, whitelist=whitelist,
                                        custom_words=custom, use_smart=not args.no_smart)
            print(f"识别到 {len(analysis.findings)} 个实体 / {analysis.occurrences} 处：")
            for f in analysis.findings:
                print(f"  [{f['category_cn']}|{f['source']}] {f['text']}  ×{f['count']}")
            return
        out_dir = args.out_dir or args.docx.parent
        if args.password and not HAS_CRYPTO:
            raise SystemExit("加密映射需要先 pip install cryptography")
        result = redact_document(
            args.docx, out_dir, mode=mode, strategies=strategies,
            password=args.password, vocab=vocab, whitelist=whitelist,
            custom_words=custom, use_smart=not args.no_smart,
        )
        for item in result["items"]:
            print(f"  [{item['category_cn']}|{item['strategy_cn']}] "
                  f"{item['original']} → {item['replacement'] or '(已删除)'} ×{item['count']}")
        print(f"\n脱敏文档: {result['outputs']['redacted']}")
        print(f"映射文件: {result['outputs']['mapping']}")
        print(f"处理报告: {result['outputs']['report']}")
        if result["warnings"]:
            print("防漏扫描警告：")
            for w in result["warnings"]:
                print("  -", w)
    else:
        result = restore_document(args.docx, args.mapping, args.out, args.password)
        print(f"还原 {result['replaced']} 处，输出: {result['output']}")


if __name__ == "__main__":
    main()
