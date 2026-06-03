import http.server
import socketserver
import urllib.parse
import sys

PORT = 8080

class AuthHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/callback':
            query = urllib.parse.parse_qs(parsed_path.query)
            if 'code' in query:
                code = query['code'][0]
                print(f"OAUTH_CODE_RECEIVED={code}")
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Success!</h1><p>I have received the code. You can close this window and go back to the chat.</p></body></html>")
                
                # We got what we needed, shut down the server
                def kill_me():
                    self.server.shutdown()
                import threading
                threading.Thread(target=kill_me).start()
                return
                
        self.send_response(404)
        self.end_headers()

with socketserver.TCPServer(("", PORT), AuthHandler) as httpd:
    print(f"Listening on port {PORT} for the Zoho callback...")
    httpd.serve_forever()
    print("Server shut down.")
