"""Local dev runner: python run.py"""
from t2.web.app import create_app

if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=True)
