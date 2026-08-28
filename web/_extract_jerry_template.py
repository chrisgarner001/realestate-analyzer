import json
import re

path = r"G:\Shared drives\PropMind AI\PropMindAI\RE Investor Analyzer Tool\Agent Email Template\PropMind Agent Invite - Jerry Johnson.html"
with open(path, encoding="utf-8") as f:
    content = f.read()

match = re.search(r'<script type="__bundler/template">\s*(.*?)\s*</script>', content, re.DOTALL)
if not match:
    print("NOT_FOUND")
else:
    template_json = match.group(1)
    template_html = json.loads(template_json)
    out_path = r"G:\My Drive\CHRIS Folder\Claude Folder\Real Estate\realestate-skills\web\_jerry_template_extracted.html"
    with open(out_path, "w", encoding="utf-8") as out:
        out.write(template_html)
    print(f"extracted_length={len(template_html)}")
    print(f"written_to={out_path}")
