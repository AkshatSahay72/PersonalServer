#!/usr/bin/env python3
import urllib.request
import urllib.error
import json

base_url = 'http://100.85.108.5:8080'

print('==============================================')
print(' 1. CREATE FOLDER ON NODE 02 VIA NODE 01 PROXY')
print('==============================================')
req = urllib.request.Request(
    f'{base_url}/storage/mkdir?node=node-02',
    data=json.dumps({'name': 'remote-write-test'}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
res = json.loads(urllib.request.urlopen(req).read().decode('utf-8'))
print('Result:', res)
assert res.get('status') == 'created'

print('\n==============================================')
print(' 2. UPLOAD TEST FILE TO NODE 02 VIA NODE 01 PROXY')
print('==============================================')
boundary = '----WebKitFormBoundaryTest1234'
file_bytes = b'Node 02 remote write test successful!'
body = (
    f'--{boundary}\r\n'
    f'Content-Disposition: form-data; name="file"; filename="test_remote.txt"\r\n'
    f'Content-Type: text/plain\r\n\r\n'
).encode('utf-8') + file_bytes + f'\r\n--{boundary}--\r\n'.encode('utf-8')

up_req = urllib.request.Request(
    f'{base_url}/storage/upload?node=node-02&path=remote-write-test',
    data=body,
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    method='POST'
)
up_res = json.loads(urllib.request.urlopen(up_req).read().decode('utf-8'))
print('Upload Result:', up_res)
assert 'test_remote.txt' in up_res.get('files', [])

print('\n==============================================')
print(' 3. LIST DIRECTORY ON NODE 02')
print('==============================================')
list_req = urllib.request.Request(f'{base_url}/storage/list?node=node-02&path=remote-write-test')
list_res = json.loads(urllib.request.urlopen(list_req).read().decode('utf-8'))
print('List Result:', list_res)
assert any(item['name'] == 'test_remote.txt' for item in list_res.get('items', []))

print('\n==============================================')
print(' 4. DOWNLOAD TEST FILE FROM NODE 02')
print('==============================================')
dl_req = urllib.request.Request(f'{base_url}/storage/download?node=node-02&path=remote-write-test/test_remote.txt')
with urllib.request.urlopen(dl_req) as resp:
    dl_data = resp.read()
    print('Downloaded data:', dl_data.decode('utf-8'))
    assert dl_data == file_bytes

print('\n==============================================')
print(' 5. RENAME TEST FILE ON NODE 02')
print('==============================================')
ren_req = urllib.request.Request(
    f'{base_url}/storage/rename?node=node-02',
    data=json.dumps({'path': 'remote-write-test/test_remote.txt', 'new_name': 'test_remote_renamed.txt'}).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
ren_res = json.loads(urllib.request.urlopen(ren_req).read().decode('utf-8'))
print('Rename Result:', ren_res)
assert ren_res.get('status') == 'renamed'

print('\n==============================================')
print(' 6. VERIFY RENAMED FILE ON NODE 02')
print('==============================================')
list_req2 = urllib.request.Request(f'{base_url}/storage/list?node=node-02&path=remote-write-test')
list_res2 = json.loads(urllib.request.urlopen(list_req2).read().decode('utf-8'))
print('List after rename:', list_res2)
assert any(item['name'] == 'test_remote_renamed.txt' for item in list_res2.get('items', []))

print('\n==============================================')
print(' 7. DELETE RENAMED FILE ON NODE 02')
print('==============================================')
del_file_req = urllib.request.Request(
    f'{base_url}/storage?node=node-02&path=remote-write-test/test_remote_renamed.txt',
    method='DELETE'
)
del_file_res = json.loads(urllib.request.urlopen(del_file_req).read().decode('utf-8'))
print('Delete File Result:', del_file_res)
assert del_file_res.get('status') == 'deleted'

print('\n==============================================')
print(' 8. DELETE TEST DIRECTORY ON NODE 02')
print('==============================================')
del_dir_req = urllib.request.Request(
    f'{base_url}/storage?node=node-02&path=remote-write-test',
    method='DELETE'
)
del_dir_res = json.loads(urllib.request.urlopen(del_dir_req).read().decode('utf-8'))
print('Delete Dir Result:', del_dir_res)
assert del_dir_res.get('status') == 'deleted'

print('\n==============================================')
print(' 9. VERIFY CLEANUP ON NODE 02')
print('==============================================')
final_list_req = urllib.request.Request(f'{base_url}/storage/list?node=node-02')
final_list_res = json.loads(urllib.request.urlopen(final_list_req).read().decode('utf-8'))
print('Final Node 02 Root Items:', final_list_res.get('items', []))
assert not any(item['name'] == 'remote-write-test' for item in final_list_res.get('items', []))

print('\n>>> ALL 9 DEDICATED REMOTE WRITE TESTS PASSED 100%! <<<')
