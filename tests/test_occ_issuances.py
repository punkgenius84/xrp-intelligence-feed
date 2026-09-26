from datetime import datetime, timezone
from pathlib import Path
import pytest
from discovery.dispatch import DiscoveryRegistryError, collect_source, load_discovery_sources, validate_discovery_sources
from discovery.http import HttpResponse
from discovery.occ_issuances import OCC_SOURCE_ID,OCC_ROOT,OCCYearlyIssuancesDiscovery,_archive_url,_parse_archive

STAMP=datetime(2026,9,24,12,tzinfo=timezone.utc)
FIXTURE=Path(__file__).parent/"fixtures"/"occ_issuances.html"

def source(**changes):
    value={"source_id":OCC_SOURCE_ID,"name":"Office of the Comptroller of the Currency","authority_tier":1,
           "category":"regulatory","discovery_method":"occ_yearly_issuances","source_url":OCC_ROOT,
           "enabled":True,"lookback_days":60,"max_items":50,"max_years":2,"document_types":["news_release","bulletin"]}
    value.update(changes); return value

def response(content,status=200,headers=None):
    return HttpResponse(status,headers or {"content-type":"text/html","etag":'"occ"'},content,OCC_ROOT)

class FakeHttp:
    def __init__(self,responses): self.responses=list(responses); self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs)); result=self.responses.pop(0)
        if isinstance(result,Exception): raise result
        return result

def test_registry_and_dispatch():
    configured=load_discovery_sources()
    occ=next(x for x in configured if x["source_id"]==OCC_SOURCE_ID)
    assert occ==source()
    result=collect_source(occ,http=FakeHttp([response(FIXTURE.read_bytes()),response(FIXTURE.read_bytes())]),now=lambda:STAMP)
    assert result.status=="success" and len(result.candidates)==2
    assert result.candidates[0].source_native_id=="NR 2026-80"

@pytest.mark.parametrize("changes",[{"source_url":"https://evil.example/occ"},{"enabled":1},{"max_years":3},{"lookback_days":0},{"document_types":["bulletin"]}])
def test_invalid_config(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version":1,"sources":[source(**changes)]})

def test_parser_extracts_native_ids_and_rejects_unknown_route():
    rows=_parse_archive(FIXTURE.read_bytes(),"news_release")
    assert [r["native_id"] for r in rows]==["NR 2026-80","NR 2026-69"]
    assert _archive_url(2026,"news_release").endswith("/2026-news-releases.html")
    assert _archive_url(2026,"bulletin").endswith("/2026-bulletins.html")

def test_lookback_filters_old_rows():
    old=b'<html><body><table><tr><td>01/01/2026</td><td>NR 2026-1</td><td><a href="/news-issuances/news-releases/2026/nr-occ-2026-1.html">Old</a></td></tr></table></body></html>'
    result=OCCYearlyIssuancesDiscovery(source(),http=FakeHttp([response(old),response(FIXTURE.read_bytes())]),now=lambda:STAMP).collect()
    assert result.status=="success"
    assert all(x.published_at.date().isoformat()>="2026-07-26" for x in result.candidates)

def test_304_skips_one_archive_and_continues():
    result=OCCYearlyIssuancesDiscovery(source(),http=FakeHttp([response(b"",304,{"etag":'"new"'}),response(FIXTURE.read_bytes())]),now=lambda:STAMP).collect(
        {"sources":{OCC_SOURCE_ID:{"requests":{"news_release:2026":{"etag":'"old"'}}}}})
    assert result.status=="success" and len(result.candidates)==2

def test_malformed_archive_fails_closed():
    result=OCCYearlyIssuancesDiscovery(source(),http=FakeHttp([response(b"<html><body>nothing</body></html>")]),now=lambda:STAMP).collect()
    assert result.status=="failed" and "no recognizable rows" in result.errors[0]

def test_candidate_identity_uses_native_occ_id():
    result=OCCYearlyIssuancesDiscovery(source(),http=FakeHttp([response(FIXTURE.read_bytes()),response(FIXTURE.read_bytes())]),now=lambda:STAMP).collect()
    assert {x.source_native_id for x in result.candidates} == {"NR 2026-80", "NR 2026-69", "OCC 2026-28", "OCC 2026-24"}
