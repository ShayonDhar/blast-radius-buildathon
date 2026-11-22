# Blast Radius selector. The coverage map (maps.json.gz) is committed; it is built
# from the pinned sqlglot commit by build_maps.py (see README). The image only
# needs the sqlglot source at that commit to compare a patched file with the original, and its
# bytecode so the selector can import a patched copy cheaply (import probe).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
RUN python -c "import io,tarfile,urllib.request as u; d=u.urlopen('https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa').read(); t=tarfile.open(fileobj=io.BytesIO(d)); ms=[m for m in t.getmembers() if m.name.split('/',1)[-1].startswith(('sqlglot/','tests/')) and m.name.endswith('.py')]; [setattr(m,'name',m.name.split('/',1)[1]) for m in ms]; t.extractall('/app/src', members=ms)" \
    && python -m compileall -q /app/src/sqlglot \
    && chmod -R a+rX /app/src
COPY selector.py maps.json.gz /app/
RUN chmod a+r /app/selector.py /app/maps.json.gz
CMD ["python", "/app/selector.py"]
