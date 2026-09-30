"""Offline acceptance tests. All output uses the retained tmp_path fixture."""
import asyncio
import base64
from pathlib import Path

import pytest

from paper_search_mcp import comm_wos, comm_download, comm_server, comm_workflow
from paper_search_mcp.comm_wos_host import parse_export, resource_url, upstream_url, wos_url, search_expression, PDFCapture

TEXT = '''FN Clarivate Web of Science
VR 1.0
PT J
AU Smith, A
   Jones, B
AF Smith, Alice
   Jones, Bob
TI International communication and
   social media
SO JOURNAL OF COMMUNICATION
LA English
DT Article
DE news; public opinion
AB A multiline
   abstract.
TC 8
PY 2024
VL 74
IS 2
BP 101
EP 118
DI 10.1234/EXAMPLE
UT WOS:000123456700001
ER

EF
'''


def test_parse_full_record_preserves_bibliography():
    p = parse_export('\ufeff' + TEXT)[0]
    assert p['source'] == 'wos' and p['language'] == 'en'
    assert p['title'] == 'International communication and social media'
    assert p['authors'] == ['Smith, Alice', 'Jones, Bob']
    assert p['doi'] == '10.1234/example' and p['published_date'] == '2024'
    assert p['abstract'] == 'A multiline abstract.'
    assert p['extra']['page'] == '101-118' and p['citations'] == 8
    assert p['extra']['creators'][0] == {'creatorType':'author','lastName':'Smith','firstName':'Alice'}
    assert parse_export('<html>Sign in</html>') == []
    assert parse_export(TEXT.replace('UT WOS:000123456700001', 'UT invalid')) == []


def test_public_codec_matches_observed_wos_entry_and_rejects_other_hosts():
    url = 'http://www.webofscience.com/'
    encoded = resource_url(url)
    assert encoded.endswith('/http/77726476706e69737468656265737421e7e056d230356a5f781b8aa59d5b20301c1db852/')
    assert upstream_url(encoded) == url and wos_url(encoded) == url
    assert wos_url(resource_url('https://webofscience.clarivate.cn/wos/')) == 'https://webofscience.clarivate.cn/wos/'
    with pytest.raises(ValueError):
        wos_url(resource_url('https://example.com/'))


@pytest.mark.parametrize('url', ['file:///x', 'http://127.0.0.1/', 'https://user:pass@webofscience.com/', 'https://webofscience.com:8000/', 'https://localhost/', 'https://x.internal/'])
def test_url_validation(url):
    with pytest.raises(ValueError):
        resource_url(url)


def test_topic_query_and_year_language_scope():
    assert search_expression('"international communication"', 2023, 2026) == 'TS=("international communication") AND PY=(2023-2026) AND LA=(English)'
    with pytest.raises(ValueError):
        search_expression('news', 2025, 2020)


def test_default_tools_are_exactly_the_simplified_surface():
    names = {t.name for t in asyncio.run(comm_server.mcp.list_tools())}
    assert names == {'comm_get_profile','comm_cnki_authenticate','comm_cnki_access_status',
                     'comm_wos_authenticate','comm_wos_access_status','comm_wos_search',
                     'comm_search_bilingual','comm_rank','comm_save_selection','comm_status',
                     'comm_zotero_probe','comm_zotero_authorize','comm_import_to_zotero','comm_retry',
                     'comm_download','comm_download_batch','comm_read_pdf','comm_export_ris'}
    assert comm_server.comm_get_profile()['available_sources'] == ['cnki', 'wos']
    assert comm_server.comm_get_profile()['profile']['default_sources'] == ['wos']


def test_wos_query_merging_and_auth_failure_are_explicit(monkeypatch):
    calls = []
    async def call(op, args):
        calls.append((op,args))
        if len(calls) == 1:
            return {'success':True,'status':'ok','papers':parse_export(TEXT)}
        return {'success':False,'authentication_required':True,'status':'needs_attention','papers':[]}
    monkeypatch.setattr(comm_wos.comm_cnki, 'call', call)
    result = asyncio.run(comm_wos.search_many(['news','media','third'], 'communication', year_start=2024, year_end=2024))
    assert len(calls) == 2 and all(c[0] == 'wos_search' for c in calls)
    assert result['status'] == 'partial' and result['count'] == 1
    assert result['searches'][-1]['sources']['wos']['authentication_required']
    with pytest.raises(ValueError, match='WoS only'):
        asyncio.run(comm_wos.search_many(['news'], 'news', sources=['openalex']))


def test_bilingual_uses_cnki_and_wos_and_preserves_auth_diagnostic(monkeypatch):
    calls=[]
    async def cnki(**args):
        calls.append('cnki')
        return {'success':False,'authentication_required':True,'authentication_stage':'webvpn_login','papers':[]}
    async def wos(**args):
        calls.append('wos')
        return {'status':'needs_attention','searches':[], 'papers':[]}
    monkeypatch.setattr(comm_server.comm_cnki, 'search', cnki)
    monkeypatch.setattr(comm_server.comm_wos, 'search_many', wos)
    r=asyncio.run(comm_server.comm_search_bilingual('国际传播', '国际传播', ['international communication']))
    assert set(calls)=={'cnki','wos'}
    assert r['sources']['zh']['status']=='needs_attention'
    assert r['sources']['zh']['authentication_required']
    assert r['sources']['en']['status']=='needs_attention'


def test_wos_download_routing_and_metadata_stay_with_wos(monkeypatch):
    p=parse_export(TEXT)[0]
    monkeypatch.setattr(comm_wos, 'download_sync', lambda record, *_: {'source':'wos','paper':record})
    monkeypatch.setattr(comm_download, 'download_one', lambda *_: pytest.fail('legacy downloader'))
    monkeypatch.setattr(comm_workflow, 'fetch_json', lambda *_: pytest.fail('metadata source drift'))
    assert comm_download.download_selected(p)['source']=='wos'
    assert comm_workflow.enrich(p)['authors']==p['authors']


def test_wos_receipt_retains_bad_pdf_without_claiming_download(monkeypatch,tmp_path):
    monkeypatch.setenv('COMM_MCP_DATA_DIR',str(tmp_path))
    path=tmp_path/'failed.pdf'
    path.write_bytes(b'<html>Access denied</html>')
    async def call(*_):
        return {'success':True,'file_path':str(path)}
    monkeypatch.setattr(comm_wos.comm_cnki,'call',call)
    r=asyncio.run(comm_wos.download(parse_export(TEXT)[0]))
    assert r['status']=='identity_unverified' and 'pdf_path' not in r
    assert Path(r['retained_path']).exists() and Path(r['receipt_path']).exists()


def test_native_pdf_capture_retains_bytes_and_aborts_disposable_download(tmp_path):
    class CDP:
        def __init__(self): self.calls=[]
        async def send(self,method,args=None):
            self.calls.append(method)
            if method=='Fetch.getResponseBody': return {'body':base64.b64encode(b'%PDF-test').decode(),'base64Encoded':True}
    c=PDFCapture(tmp_path); c.cdp=CDP()
    asyncio.run(c._response({'requestId':'1','responseStatusCode':200,'responseHeaders':[{'name':'Content-Type','value':'application/pdf'}]}))
    assert c.path.read_bytes()==b'%PDF-test'
    assert 'Fetch.failRequest' in c.cdp.calls and 'Fetch.continueRequest' not in c.cdp.calls


def test_ris_export_uses_wos_full_record(monkeypatch,tmp_path):
    monkeypatch.setenv('COMM_MCP_DATA_DIR',str(tmp_path))
    r=comm_server.comm_export_ris(parse_export(TEXT))
    text=Path(r['path']).read_text(encoding='utf-8')
    for value in ['AU  - Smith, Alice', 'DO  - 10.1234/example', 'PY  - 2024', 'SP  - 101-118']:
        assert value in text


@pytest.mark.parametrize('range_header, retained', [('bytes 0-8/9',True),('bytes 0-8/90',False)])
def test_pdf_206_only_accepts_a_complete_file(tmp_path,range_header,retained):
    class CDP:
        async def send(self,method,args=None):
            if method=='Fetch.getResponseBody': return {'body':base64.b64encode(b'%PDF-test').decode(),'base64Encoded':True}
    c=PDFCapture(tmp_path); c.cdp=CDP()
    event={'requestId':'1','responseStatusCode':206,'responseHeaders':[{'name':'Content-Type','value':'application/pdf'},{'name':'Content-Range','value':range_header}]}
    asyncio.run(c._response(event))
    assert bool(c.path)==retained


def test_pdf_abort_failure_keeps_file_and_detaches_even_if_disable_fails(tmp_path):
    class CDP:
        detached=False
        async def send(self,method,args=None):
            if method=='Fetch.getResponseBody': return {'body':base64.b64encode(b'%PDF-test').decode(),'base64Encoded':True}
            raise RuntimeError('closed channel')
        async def detach(self): self.detached=True
    class Page:
        closed=False
        async def close(self): self.closed=True
    c=PDFCapture(tmp_path); c.cdp=CDP(); c.page=Page()
    async def run():
        await c._response({'requestId':'1','responseStatusCode':200,'responseHeaders':[{'name':'Content-Type','value':'application/pdf'}]})
        await c.close()
    asyncio.run(run())
    assert c.path.exists() and c.page.closed and c.cdp.detached


def test_shared_worker_joins_identical_wos_requests(monkeypatch,tmp_path):
    from paper_search_mcp import comm_cnki, comm_wos_host
    monkeypatch.setenv('COMM_MCP_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('COMM_CNKI_ACCESS_MODE','bfsu_webvpn')
    instances=[]
    class WOS:
        stage='test'
        def __init__(self,browser): self.browser=browser; self.calls=0; instances.append(self)
        async def search(self,**args):
            self.calls+=1
            await asyncio.sleep(.03)
            return {'success':True,'papers':[]}
    monkeypatch.setattr(comm_wos_host,'WOSBackend',WOS)
    worker=comm_cnki.Worker()
    async def run():
        result=await asyncio.gather(worker.call('wos_search',{'query':'news'}), worker.call('wos_search',{'query':'news'}))
        assert all(x['success'] for x in result) and instances[0].calls==1
        await worker.call('wos_search',{'query':'news'})
        assert instances[0].calls==2 and instances[0].browser.webvpn_enabled
    try: asyncio.run(run())
    finally: worker.close()
