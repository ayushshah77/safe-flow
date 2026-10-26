import ipaddress, socket, httpx2
from pathlib import Path
from urllib.parse import urlparse
from pydantic import BaseModel, Field
import trafilatura
from app.registry import action

WORKSPACE = Path("workspace").resolve()
ALLOWED_DOMAINS = {"example.com", "en.wikipedia.org", "news.ycombinator.com"}
ALLOWED_EXT = {".txt", ".md", ".json", ".csv"}

def safe_path(rel: str) -> Path:
    p = (WORKSPACE / rel).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise PermissionError("path escapes workspace")
    if p.suffix not in ALLOWED_EXT:
        raise PermissionError(f"extension {p.suffix} not allowed")
    return p

class ReadIn(BaseModel):  path: str
class ReadOut(BaseModel): text: str

@action("read_file", "Read a text file from the workspace", ReadIn, ReadOut)
def read_file(a: ReadIn) -> ReadOut:
    p = safe_path(a.path)
    if p.stat().st_size > 1_000_000:
        raise ValueError("file too large")
    return ReadOut(text=p.read_text())

class WriteIn(BaseModel):  path: str; content: str = Field(max_length=200_000)
class WriteOut(BaseModel): path: str; bytes_written: int

@action("write_file", "Write text to a workspace file", WriteIn, WriteOut, risk="write")
def write_file(a: WriteIn) -> WriteOut:
    p = safe_path(a.path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a.content)
    return WriteOut(path=a.path, bytes_written=len(a.content.encode()))

class GetIn(BaseModel):  url: str
class GetOut(BaseModel): status: int; text: str

def _check_url(url: str):
    u = urlparse(url)
    if u.scheme != "https": raise PermissionError("https only")
    if u.hostname not in ALLOWED_DOMAINS: raise PermissionError("domain not allowlisted")
    for info in socket.getaddrinfo(u.hostname, 443):   # SSRF guard
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise PermissionError("private address blocked")

@action("http_get", "Fetch a web page over HTTPS and return its readable text",
        GetIn, GetOut, risk="network")
def http_get(a: GetIn) -> GetOut:
    _check_url(a.url)
    r = httpx.get(a.url, timeout=10, follow_redirects=False)
    text = trafilatura.extract(r.text) or ""
    return GetOut(status=r.status_code, text=text[:200_000])

class WcIn(BaseModel):  text: str
class WcOut(BaseModel): words: int

@action("word_count", "Count words in text", WcIn, WcOut)
def word_count(a: WcIn) -> WcOut:
    return WcOut(words=len(a.text.split()))