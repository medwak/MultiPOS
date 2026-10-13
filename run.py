"""Start MultiPOS:  python run.py   then open http://localhost:5000 (or http://<this-pc-ip>:5000 from a tablet)."""
import os

from pos import create_app

try:
    from dotenv import load_dotenv  # optional
    load_dotenv()
except ImportError:
    pass

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    try:
        from waitress import serve  # production-grade server if installed
        print(f"MultiPOS running on http://{host}:{port}")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=os.environ.get("DEBUG") == "1", threaded=True)
