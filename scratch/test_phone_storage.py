#!/usr/bin/env python3
import urllib.request
import urllib.parse
import urllib.error
import json
import os
import sys

base = 'http://127.0.0.1:8080'

print("Running Storage Subsystem Test on Phone...")

# 1. Usage
usage_res = json.loads(urllib.request.urlopen(base + '/storage/usage').read().decode())
print('Storage Root:', usage_res['storage_root'])
print('Initial Files:', usage_res['files_count'])

# 2. Mkdir
req = urllib.request.Request(base + '/storage/mkdir', data=json.dumps({'name': 'test_docs'}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
mkdir_res = json.loads(urllib.request.urlopen(req).read().decode())
print('Created Dir:', mkdir_res['path'])

# 3. Upload File into test_docs
file_content = b'Hello PersonalServer v1.0 Storage!'
boundary = '----Boundary12345'
body = (
    f'--{boundary}\r\n'
    f'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\n'
    f'Content-Type: text/plain\r\n\r\n'
).encode() + file_content + f'\r\n--{boundary}--\r\n'.encode()

up_req = urllib.request.Request(
    base + '/storage/upload?path=test_docs',
    data=body,
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    method='POST'
)
up_res = json.loads(urllib.request.urlopen(up_req).read().decode())
print('Uploaded File:', up_res['files'])

# 4. List Directory
list_res = json.loads(urllib.request.urlopen(base + '/storage/list?path=test_docs').read().decode())
print('Listed Items:', [item['name'] for item in list_res['items']])
assert any(item['name'] == 'hello.txt' for item in list_res['items']), 'hello.txt must be listed'

# 5. Download File
dl_res = urllib.request.urlopen(base + '/storage/download?path=test_docs/hello.txt').read()
assert dl_res == file_content, f'Downloaded content mismatch: {dl_res}'
print('Download verified, bytes matched:', len(dl_res))

# 6. Rename File
ren_req = urllib.request.Request(
    base + '/storage/rename',
    data=json.dumps({'path': 'test_docs/hello.txt', 'new_name': 'renamed_hello.txt'}).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
ren_res = json.loads(urllib.request.urlopen(ren_req).read().decode())
print('Renamed File:', ren_res)

# 7. Delete File & Directory
del_req = urllib.request.Request(base + '/storage?path=test_docs/renamed_hello.txt', method='DELETE')
del_res = json.loads(urllib.request.urlopen(del_req).read().decode())
print('Deleted File:', del_res['path'])

del_dir_req = urllib.request.Request(base + '/storage?path=test_docs', method='DELETE')
del_dir_res = json.loads(urllib.request.urlopen(del_dir_req).read().decode())
print('Deleted Dir:', del_dir_res['path'])

# 8. Path Traversal Defenses Test
traversal_payloads = [
    '../',
    '../../config/secrets/enrollment.token',
    '..\\..\\config\\node.conf',
    '/etc/passwd',
    '/data/data/com.termux/files/home/.ssh/id_rsa',
    'test/../../../etc/shadow'
]

for payload in traversal_payloads:
    try:
        url = base + '/storage/list?path=' + urllib.parse.quote(payload)
        urllib.request.urlopen(url)
        print('SECURITY VULNERABILITY: Traversal payload allowed:', payload)
        sys.exit(1)
    except urllib.error.HTTPError as e:
        assert e.code in (403, 400), f'Expected 403/400 Forbidden for {payload}, got {e.code}'

print('All 6 Path Traversal attack vectors REJECTED (HTTP 403/400).')
print("STORAGE SUBSYSTEM ON PHONE: 100% PASS")
