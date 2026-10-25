"""Traditional to simplified Chinese, for the guardrail rules only (N-213).

The rules in ``injection.py`` and ``judge.py`` are written in simplified Chinese, so
"忽略之前的規則" or "無視以上所有指令" matched nothing and a traditional-script knowledge
document went straight into the index. ``normalize`` folds traditional characters with this
table before matching.

The table is not a converter: it holds only the characters that fold onto a character the rules
use (``SOURCE``), taken from OpenCC's TSCharacters.txt (Apache-2.0, first value of each one to
one entry). A character the rules use is never folded, even when OpenCC lists it as traditional
("覆盖" must stay "覆盖", not "复盖"). Two hand-kept additions cover what a one-to-one table
cannot: 帳 and the variant 帐 fold onto 账 (OpenCC gives 帐, which no rule uses; 轉帳 and 帳戶 are
the Taiwan and Hong Kong spelling), and 回覆 and 答覆 become 回复 and 答复 as words, since 覆 itself
stays. When a rule gains a character, regenerate:

    python -m app.guardrails.hant TSCharacters.txt

``tests/test_traditional_injection.py`` fails until the table is regenerated.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Characters of the rule files when the table was generated.
SOURCE = (
    "一上下不与专丢个中为主义之二于些交人从他付代令以们任份仿会传伪位何作你例供便保信"
    "修借值假做偿储元充先入全公其具内冒写决况冻准凡凭出击函分切列则刚判别到制前加务动"
    "助勿包卡即原去及友反发取受口句只台号司同名后向否启告员命和咨品售商器回图在址均坐"
    "域填处复外天失奖套她好如始子字它安官定宝审客容密寨对导将小就属山工帝席平并应废开"
    "式引当录往待律得微心必忘忽态急总息您情惕意我或户所手才打执扫扮批找把抛报押拒拨持"
    "指换据掉接控描提撇支收改效数文料断新方无旦明易是智有服未本机束来果某校样核框档模"
    "款止正此段比求汇没法活消添渠演激点照片物狱现理生用由申电略登的盖直相知短码确示礼"
    "禁私种积称税空立站等答算管类系索红约级纳终绑结绕给络绝统续维缴网置群者聊联能自至"
    "致若英范蓄虚行补要覆规视解警认让记设访证诈诉词试话询该说请调谨账购费资赔起超越跳"
    "身转轻输过运返这进述退送通造遇道部都醒里金钓钱银链问间防陌限随需非面须顾领风马验"
    "骗鱼"
)
# Traditional character, then the simplified one it folds onto.
PAIRS = (
    "丟丢並并併并來来係系俬私個个們们偽伪傳传僞伪償偿儲储內内凍冻別别則则剛刚動动務务"
    "匯汇卽即員员問问啓启啟启噹当嚮向圖图執执報报奬奖審审寫写寶宝將将專专對对導导屬属"
    "幷并廢废彆别彔录彙汇後后從从復复態态憑凭應应戶户拋抛掃扫換换撥拨擊击據据數数斷断"
    "於于曏向會会樣样機机檔档檯台決决沒没況况準准滙汇為为無无爲为獄狱獎奖現现當当發发"
    "確确碼码禮礼稅税種种稱称積积箇个範范約约紅红納纳級级終终結结絕绝絡络給给統统絶绝"
    "綁绑維维網网緻致總总繞绕繫系繳缴續续纔才羣群義义聯联脩修臺台與与蓆席蓋盖處处虛虚"
    "號号衕同裏里補补裡里製制複复覈核規规視视記记訪访設设訴诉証证詐诈詞词詢询試试話话"
    "該该認认說说説说調调請请諮咨謹谨證证讓让費费資资賠赔賬账購购輕轻輸输轉转迴回這这"
    "進进運运過过遶绕釣钓銀银錄录錢钱録录鏈链開开間间隨随隻只電电須须領领類类顧顾風风"
    "颱台馬马騙骗驗验髮发鬚须魚鱼麪面麫面麵面點点齣出"
)
EXTRA = "帳账帐账"
WORDS = (("回覆", "回复"), ("答覆", "答复"))
FOLD = str.maketrans(PAIRS[0::2] + EXTRA[0::2], PAIRS[1::2] + EXTRA[1::2])


def fold(text: str) -> str:
    """Traditional characters onto the simplified ones the rules use; the length never changes."""
    text = text.translate(FOLD)
    for word, simplified in WORDS:
        text = text.replace(word, simplified)
    return text

_RULE_FILES = ("injection.py", "judge.py")
_CJK = re.compile(r"[㐀-鿿]")


def rule_chars() -> set[str]:
    """Characters in the string literals of the rule files; comments and docstrings do not count."""
    here = Path(__file__).parent
    chars: set[str] = set()
    for name in _RULE_FILES:
        tree = ast.parse((here / name).read_text(encoding="utf-8"))
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                chars.update(_CJK.findall(node.value))
    return chars


def _generate(dictionary: str) -> None:
    vocab = rule_chars()
    pairs = {}
    for line in Path(dictionary).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, values = line.split("\t")
        value = values.split()[0]
        if len(key) == len(value) == 1 and key != value and value in vocab and key not in vocab:
            pairs[key] = value
    source = "".join(sorted(vocab))
    flat = "".join(k + v for k, v in sorted(pairs.items()))

    def wrap(text: str, width: int) -> str:
        return '"\n    "'.join(text[i:i + width] for i in range(0, len(text), width))

    path = Path(__file__)
    body = path.read_text(encoding="utf-8")
    body = re.sub(r'SOURCE = \(\n    ".*?"\n\)', lambda _: f'SOURCE = (\n    "{wrap(source, 40)}"\n)', body, flags=re.S)
    body = re.sub(r'PAIRS = \(\n    ".*?"\n\)', lambda _: f'PAIRS = (\n    "{wrap(flat, 40)}"\n)', body, flags=re.S)
    path.write_text(body, encoding="utf-8")
    print(f"{len(vocab)} rule characters, {len(pairs)} traditional characters")


if __name__ == "__main__":
    _generate(sys.argv[1])
