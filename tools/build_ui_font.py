"""Optional release tool: pip install fonttools brotli (not runtime dependencies)."""
from pathlib import Path
from fontTools import subset
from fontTools.ttLib import TTFont

root = Path(__file__).resolve().parents[1]
fonts = root / 'posthub/static/fonts'
characters = set(chr(i) for i in range(32, 592))
characters.update('年月日时分秒星期一二三四五六七八九十上午下午')
for path in (root / 'posthub').rglob('*'):
    if path.suffix in {'.py', '.js', '.html'}:
        characters.update(path.read_text(encoding='utf-8'))
font = TTFont(fonts / 'NotoSansSC-variable.woff2')
options = subset.Options()
options.flavor = 'woff2'
subsetter = subset.Subsetter(options=options)
subsetter.populate(unicodes={ord(c) for c in characters})
subsetter.subset(font)
output = fonts / 'NotoSansSC-ui.woff2'
font.save(output)
print('UI font subset:', output.stat().st_size, 'bytes;', len(characters), 'characters')
