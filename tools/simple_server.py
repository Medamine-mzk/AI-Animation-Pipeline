import http.server, json, pathlib, uuid, os
ROOT=pathlib.Path(__file__).resolve().parents[1]
JOBS=ROOT/"jobs"
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT),**kw)
    def do_POST(self):
        if self.path=="/api/jobs":
            length=int(self.headers.get('Content-Length',0))
            data=self.rfile.read(length)
            fname=self.headers.get('X-Filename','input.wav')
            job_id=str(uuid.uuid4())[:8]
            job_dir=JOBS/job_id; job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir/"input.wav").write_bytes(data)
            (job_dir/"status.json").write_text(json.dumps({"status":"done"}))
            (job_dir/"meta.json").write_text(json.dumps({"filename":fname,"size":len(data)}))
            import shutil
            golden=ROOT/"jobs/golden/transcript.json"
            if golden.exists():
                try: shutil.copy(golden, job_dir/"transcript.json")
                except: pass
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"job_id":job_id,"status":"done"}).encode())
            return
        if self.path=="/save":
            length=int(self.headers.get('Content-Length',0))
            body=self.rfile.read(length)
            out=ROOT/"jobs/picker/picker.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
            return
        super().do_POST()
    def do_GET(self):
        if self.path.startswith("/api/jobs/"):
            parts=self.path.split("/")
            if len(parts)>=4:
                job_id=parts[3]
                p=JOBS/job_id/"status.json"
                if p.exists():
                    self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                    self.wfile.write(p.read_bytes()); return
            self.send_response(404); self.end_headers(); return
        return super().do_GET()

if __name__=="__main__":
    import socketserver
    with socketserver.TCPServer(("",8000), Handler) as httpd:
        print("Serving at 8000")
        httpd.serve_forever()
