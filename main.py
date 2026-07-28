from sixv.app import create_app


app = create_app()


if __name__ == "__main__":
    settings = app.extensions["sixv_settings"]
    app.run(host=settings.host, port=settings.port, threaded=True, use_reloader=False)
