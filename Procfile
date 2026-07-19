admin: streamlit run admin/app.py --server.port $PORT --server.address 0.0.0.0
portal: gunicorn portal.app:app --bind 0.0.0.0:$PORT
