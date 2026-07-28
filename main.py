from ainas.app import create_app


app = create_app()


if __name__ == "__main__":
    settings = app.extensions["video_station_settings"]
    app.run(host=settings.host, port=settings.port, threaded=True, use_reloader=False)
