"""Run isolated browser workflows with Playwright-managed Chromium."""
import os,subprocess,sys
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
work=root/'artifacts/browser-work';work.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:browser=p.chromium.executable_path
for name in ['onboarding','account','app_channels','help','weixin_recovery']:
    subprocess.run([sys.executable,str(root/'tests'/('browser_'+name+'.py')),'--browser',browser,'--work-dir',str(work),'--artifacts',str(root/'artifacts'/('browser-'+name))],cwd=root,check=True,env=os.environ|{'PYTHONIOENCODING':'utf-8'})
