#!/usr/bin/env python3
import urllib.request
import urllib.error
import json

base_url = 'http://100.85.108.5:8080'

print('==============================================')
print(' SECURITY TEST 1: Path Traversal (../escape)')
print('==============================================')
try:
    req = urllib.request.Request(
        f'{base_url}/storage/upload?node=node-02&path=../../etc',
        data=b'hello',
        headers={'Content-Type': 'application/octet-stream'},
        method='POST'
    )
    urllib.request.urlopen(req)
    print('FAILED: Path traversal was permitted!')
    exit(1)
except urllib.error.HTTPError as e:
    print(f'PASS: Path traversal rejected with HTTP {e.code}: {e.read().decode("utf-8")}')
    assert e.code in (400, 403)

print('\n==============================================')
print(' SECURITY TEST 2: Absolute Path Escape (/etc/test)')
print('==============================================')
try:
    req = urllib.request.Request(
        f'{base_url}/storage/mkdir?node=node-02&path=/etc',
        data=json.dumps({'name': 'test_escape'}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req)
    print('FAILED: Absolute path escape was permitted!')
    exit(1)
except urllib.error.HTTPError as e:
    print(f'PASS: Absolute path rejected with HTTP {e.code}: {e.read().decode("utf-8")}')
    assert e.code in (400, 403)

print('\n==============================================')
print(' SECURITY TEST 3: ~/.ssh Escape Attempt')
print('==============================================')
try:
    req = urllib.request.Request(
        f'{base_url}/storage/list?node=node-02&path=../.ssh',
        method='GET'
    )
    urllib.request.urlopen(req)
    print('FAILED: ~/.ssh escape was permitted!')
    exit(1)
except urllib.error.HTTPError as e:
    print(f'PASS: ~/.ssh escape rejected with HTTP {e.code}: {e.read().decode("utf-8")}')
    assert e.code in (400, 403)

print('\n==============================================')
print(' SECURITY TEST 4: Storage Root Deletion')
print('==============================================')
try:
    req = urllib.request.Request(
        f'{base_url}/storage?node=node-02&path=',
        method='DELETE'
    )
    urllib.request.urlopen(req)
    print('FAILED: Root storage deletion was permitted!')
    exit(1)
except urllib.error.HTTPError as e:
    print(f'PASS: Root deletion rejected with HTTP {e.code}: {e.read().decode("utf-8")}')
    assert e.code in (400, 403)

print('\n==============================================')
print(' SECURITY TEST 5: Unknown Node ID Rejection')
print('==============================================')
try:
    req = urllib.request.Request(
        f'{base_url}/storage/mkdir?node=unknown-fake-node-999',
        data=json.dumps({'name': 'test_fake'}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req)
    print('FAILED: Unknown node ID was permitted!')
    exit(1)
except urllib.error.HTTPError as e:
    print(f'PASS: Unknown node ID rejected with HTTP {e.code}: {e.read().decode("utf-8")}')
    assert e.code == 404

print('\n==============================================')
print(' SECURITY TEST 6: Arbitrary URL SSRF Rejection')
print('==============================================')
try:
    req = urllib.request.Request(
        f'{base_url}/storage/mkdir?node=http://127.0.0.1:8000',
        data=json.dumps({'name': 'test_ssrf'}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req)
    print('FAILED: SSRF target was permitted!')
    exit(1)
except urllib.error.HTTPError as e:
    print(f'PASS: SSRF target rejected with HTTP {e.code}: {e.read().decode("utf-8")}')
    assert e.code == 404

print('\n>>> ALL 6 SECURITY TESTS PASSED 100%! <<<')
