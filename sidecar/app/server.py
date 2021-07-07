from flask import Flask, request, jsonify, url_for
from celery import Celery
import os
import build_sig

app = Flask(__name__)
app.config['CELERY_BROKER_URL'] = os.environ.get("CELERY_BROKER_URL")
app.config['CELERY_RESULT_BACKEND'] = os.environ.get("CELERY_RESULT_BACKEND")

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

@celery.task(bind=True)
def config_sig(self, tunnel_params):
       state = build_sig.main(**tunnel_params)
       print(state)
       return state

@app.route('/')
def index():
    return 'Web App with Python Flask!'

@app.route('/healthy')
def healthy():
    return "I am alive"

@app.route('/configure', methods=['POST'])
def configure():
    tunnel_params = request.get_json(silent=False)
    # TODO use jsonschema to validate arguments here
    task = config_sig.delay(tunnel_params)
    print(tunnel_params)
    print(type(tunnel_params))
    return jsonify({'Location': url_for('taskstatus', task_id=task.id)}), 202


@app.route('/status/<task_id>')
def taskstatus(task_id):
    task = config_sig.AsyncResult(task_id)
    response = {
            'state': task.state,
            'result': task.get()
        }
    return jsonify(response)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
