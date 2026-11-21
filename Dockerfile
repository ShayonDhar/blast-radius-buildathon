# Blast Radius selector with runtime verification. The coverage map (maps.json.gz) is
# committed; the image carries sqlglot (sources + tests + fixtures, precompiled) at the pinned
# commit and the labelling environment's packages so the selector can run sqlglot tests.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
RUN python -c "import io,tarfile,urllib.request as u; d=u.urlopen('https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa').read(); t=tarfile.open(fileobj=io.BytesIO(d)); ms=[m for m in t.getmembers() if m.name.split('/',1)[-1].startswith(('sqlglot/','tests/'))]; [setattr(m,'name',m.name.split('/',1)[1]) for m in ms]; t.extractall('/app/src', members=ms, filter='data')" \
    && python -m compileall -q /app/src \
    && chmod -R a+rX /app
COPY selector.py verify.py maps.json.gz /app/
RUN chmod a+r /app/selector.py /app/verify.py /app/maps.json.gz
CMD ["python", "/app/selector.py"]
