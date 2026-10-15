"""Audit actual owner-supplied final artifact/QA manifest; no baked-in private paths."""
import hashlib
import json
import os
from pathlib import Path
import zipfile

def main():
    manifest=Path(os.environ['D34_ARTIFACT_MANIFEST'])
    value=json.loads(manifest.read_text(encoding='utf-8'))
    checks=[]
    for kind in ('docx','mp4'):
        item=value[kind];path=Path(item['path']);assert path.is_file() and path.stat().st_size>0
        actual=hashlib.sha256(path.read_bytes()).hexdigest();assert actual==item['sha256']
        qa=Path(item['qa_report']);assert qa.is_file();report=json.loads(qa.read_text(encoding='utf-8'))
        assert report.get('status')=='PASS' and report.get('artifact_sha256')==actual
        if kind=='docx':
            with zipfile.ZipFile(path) as archive:assert 'word/document.xml' in archive.namelist()
            assert report.get('all_pages_visually_inspected') is True
        else:assert report.get('full_decode_passed') is True and report.get('chapters_visually_inspected') is True
        checks.append({'kind':kind,'sha256':actual,'qa':'PASS'})
    screenshots=Path(value['screenshot_provenance']);assert screenshots.is_file() and screenshots.stat().st_size>0
    print(json.dumps({'artifact_audit':checks,'actual_provenance_present':True,'limitations':['QA contents require independent review.']}))
if __name__=='__main__':main()
