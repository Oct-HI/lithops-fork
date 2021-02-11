#
# Copyright Cloudlab URV 2020
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import sys
import copy
import time
import json
import uuid
import flask
import logging
import requests
import threading
from gevent.pywsgi import WSGIServer
from concurrent.futures import ThreadPoolExecutor

from lithops.constants import LITHOPS_TEMP_DIR, JOBS_DIR, \
    REMOTE_INSTALL_DIR, LT_LOG_FILE, LOGS_DIR, \
    LITHOPS_SERVICE_PORT
from lithops.storage.utils import create_job_key
from lithops.localhost.localhost import LocalhostHandler
from lithops.standalone.standalone import StandaloneHandler
from lithops.utils import verify_runtime_name, iterchunks, setup_lithops_logger
from lithops.util.ssh_client import SSHClient


logger = logging.getLogger('lithops.controller')

controller = flask.Flask(__name__)

last_usage_time = time.time()
keeper = None
jobs = {}
standalone_handler = None

PROXY_SERVICE_NAME = 'lithopsproxy.service'
PROXY_SERVICE_FILE = """
[Unit]
Description=Lithops Proxy
After=network.target

[Service]
ExecStart=/usr/bin/python3 {}/proxy.py
Restart=always

[Install]
WantedBy=multi-user.target
""".format(REMOTE_INSTALL_DIR)

START_TIMEOUT = 300

INTERNAL_SSH_CREDNTIALS = {'username': 'root', 'password': 'lithops'}
STANDALONE_CONFIG_FILE = os.path.join(REMOTE_INSTALL_DIR, 'config')
STANDALONE_CONFIG = json.loads(open(STANDALONE_CONFIG_FILE, 'r').read())

config_file = os.path.join(STANDALONE_CONFIG_FILE)
with open(config_file, 'r') as cf:
    standalone_config = json.load(cf)


def budget_keeper():
    global last_usage_time
    global jobs
    global standalone_handler

    jobs_running = False

    logger.info("BudgetKeeper started")

    if standalone_handler.auto_dismantle:
        logger.info('Auto dismantle activated - Soft timeout: {}s, Hard Timeout: {}s'
                    .format(standalone_handler.soft_dismantle_timeout,
                            standalone_handler.hard_dismantle_timeout))
    else:
        # If auto_dismantle is deactivated, the VM will be always automatically
        # stopped after hard_dismantle_timeout. This will prevent the VM
        # being started forever due a wrong configuration
        logger.info('Auto dismantle deactivated - Hard Timeout: {}s'
                    .format(standalone_handler.hard_dismantle_timeout))

    while True:
        time_since_last_usage = time.time() - last_usage_time
        check_interval = standalone_handler.soft_dismantle_timeout / 10
        for job_key in jobs.keys():
            done = os.path.join(JOBS_DIR, job_key+'.done')
            if os.path.isfile(done):
                jobs[job_key] = 'done'
        if len(jobs) > 0 and all(value == 'done' for value in jobs.values()) \
           and standalone_handler.auto_dismantle:

            # here we need to catch a moment when number of running jobs become zero.
            # when it happens we reset countdown back to soft_dismantle_timeout
            if jobs_running:
                jobs_running = False
                last_usage_time = time.time()
                time_since_last_usage = time.time() - last_usage_time

            time_to_dismantle = int(standalone_handler.soft_dismantle_timeout - time_since_last_usage)
        else:
            time_to_dismantle = int(standalone_handler.hard_dismantle_timeout - time_since_last_usage)
            jobs_running = True

        if time_to_dismantle > 0:
            logger.info("Time to dismantle: {} seconds".format(time_to_dismantle))
            time.sleep(check_interval)
        else:
            logger.info("Dismantling setup")
            try:
                standalone_handler.dismantle()
            except Exception as e:
                logger.info("Dismantle error {}".format(e))


def init_keeper():
    global keeper
    global standalone_handler
    global standalone_config

    access_data = os.path.join(REMOTE_INSTALL_DIR, 'access.data')
    with open(access_data, 'r') as ad:
        vsi_details = json.load(ad)
        logger.info("Parsed self name: {}, IP: {} and instance ID: {}"
                    .format(vsi_details['instance_name'],
                            vsi_details['ip_address'],
                            vsi_details['instance_id']))

    standalone_handler = StandaloneHandler(standalone_config)
    vsi = standalone_handler.backend.create_worker(vsi_details['instance_name'])
    vsi.ip_address = vsi_details['ip_address']
    vsi.instance_id = vsi_details['instance_id']
    vsi.delete_on_dismantle = False if 'master' in vsi_details['instance_name'] else True

    keeper = threading.Thread(target=budget_keeper)
    keeper.daemon = True
    keeper.start()


def iterchunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def is_instance_ready(ssh_client):
    """
    Checks if the VM instance is ready to receive ssh connections
    """
    try:
        ssh_client.run_remote_command('id')
    except Exception:
        ssh_client.close()
        return False
    return True


def wait_instance_ready(ssh_client):
    """
    Waits until the VM instance is ready to receive ssh connections
    """
    ip_addr = ssh_client.ip_address
    logger.info('Waiting VM instance {} to become ready'.format(ip_addr))

    start = time.time()
    while(time.time() - start < START_TIMEOUT):
        if is_instance_ready(ssh_client):
            logger.info('VM instance {} ready in {} seconds'
                        .format(ip_addr, round(time.time()-start, 2)))
            return True
        time.sleep(5)

    raise Exception('VM readiness {} probe expired. Check your master VM'.format(ip_addr))


def is_proxy_ready(ip_addr):
    """
    Checks if the proxy is ready to receive http connections
    """
    try:
        url = "http://{}:{}/ping".format(ip_addr, LITHOPS_SERVICE_PORT)
        r = requests.get(url, timeout=1)
        if r.status_code == 200:
            return True
        return False
    except Exception:
        return False


def wait_proxy_ready(ip_addr):
    """
    Waits until the proxy is ready to receive http connections
    """

    logger.info('Waiting Lithops proxy to become ready on {}'.format(ip_addr))

    start = time.time()
    while(time.time() - start < START_TIMEOUT):
        if is_proxy_ready(ip_addr):
            logger.info('Lithops proxy {} ready in {} seconds'
                        .format(ip_addr, round(time.time()-start, 2)))
            return True
        time.sleep(2)

    raise Exception('Proxy readiness probe expired on {}. Check your VM'.format(ip_addr))


def run_job_on_worker(worker_info, call_ids_range, job_payload):
    """
    Install all the Lithops dependencies into the worker.
    Runs the job
    """
    instance_name, ip_address, instance_id = worker_info
    logger.info('Going to setup {}, IP address {}'.format(instance_name, ip_address))

    ssh_client = SSHClient(ip_address, INTERNAL_SSH_CREDNTIALS)
    wait_instance_ready(ssh_client)

    # upload zip lithops package
    logger.info('Uploading lithops files to VM instance {}'.format(ip_address))
    ssh_client.upload_local_file('/opt/lithops/lithops_standalone.zip', '/tmp/lithops_standalone.zip')
    logger.info('Executing lithops installation process on VM instance {}'.format(ip_address))
    script = get_host_setup_scritp(instance_name, ip_address, instance_id)
    ssh_client.run_remote_command(script, run_async=True)
    ssh_client.close()

    # Wait until the proxy is ready
    wait_proxy_ready(ip_address)

    dbr = job_payload['data_byte_ranges']
    job_payload['call_ids'] = call_ids_range
    job_payload['data_byte_ranges'] = [dbr[int(call_id)] for call_id in call_ids_range]

    url = "http://{}:{}/run".format(ip_address, LITHOPS_SERVICE_PORT)
    r = requests.post(url, data=json.dumps(job_payload))
    response = r.json()

    if 'activationId' in response:
        logger.info('Invocation {} done. Activation ID: {}'
                    .format(', '.join(call_ids_range), response['activationId']))
    else:
        logger.error('Invocation {} failed: {}'
                     .format(', '.join(call_ids_range), response['error']))


def run_job():
    """
    Runs a given job
    """
    global STANDALONE_CONFIG

    job_payload = json.loads(sys.argv[2])
    STANDALONE_CONFIG.update(job_payload['config']['standalone'])
    exec_mode = job_payload['config']['standalone'].get('exec_mode', 'consume')

    if exec_mode == 'create':
        logger.info('Running job on worker VMs')
        call_ids = job_payload['call_ids']
        chunksize = job_payload['chunksize']
        workers = json.loads(sys.argv[3])

        with ThreadPoolExecutor(len(workers)) as executor:
            for call_ids_range in iterchunks(call_ids, chunksize):
                worker_info = workers.pop(0)
                executor.submit(run_job_on_worker,
                                worker_info,
                                call_ids_range,
                                copy.deepcopy(job_payload))

    else:
        logger.info('Running job on localhost')
        # Run the job in the local Vm instance
        url = "http://{}:{}/run".format('127.0.0.1', LITHOPS_SERVICE_PORT)
        requests.post(url, data=json.dumps(job_payload))


def error(msg):
    response = flask.jsonify({'error': msg})
    response.status_code = 404
    return response


@controller.route('/run', methods=['POST'])
def run():
    """
    Run a job
    """
    global last_usage_time
    global standalone_handler
    global jobs

    message = flask.request.get_json(force=True, silent=True)
    if message and not isinstance(message, dict):
        return error('The action did not receive a dictionary as an argument.')

    try:
        runtime = message['runtime_name']
        verify_runtime_name(runtime)
    except Exception as e:
        return error(str(e))

    last_usage_time = time.time()

    standalone_config = message['config']['standalone']
    standalone_handler.auto_dismantle = standalone_config['auto_dismantle']
    standalone_handler.soft_dismantle_timeout = standalone_config['soft_dismantle_timeout']
    standalone_handler.hard_dismantle_timeout = standalone_config['hard_dismantle_timeout']

    act_id = str(uuid.uuid4()).replace('-', '')[:12]
    executor_id = message['executor_id']
    job_id = message['job_id']
    job_key = create_job_key(executor_id, job_id)
    jobs[job_key] = 'running'

    pull_runtime = standalone_config.get('pull_runtime', False)
    localhost_handler = LocalhostHandler({'runtime': runtime, 'pull_runtime': pull_runtime})
    localhost_handler.run_job(message)

    response = flask.jsonify({'activationId': act_id})
    response.status_code = 202

    return response


@controller.route('/ping', methods=['GET'])
def ping():
    response = flask.jsonify({'response': 'pong'})
    response.status_code = 200

    return response


@controller.route('/preinstalls', methods=['GET'])
def preinstalls():

    message = flask.request.get_json(force=True, silent=True)
    if message and not isinstance(message, dict):
        return error('The action did not receive a dictionary as an argument.')

    try:
        runtime = message['runtime']
        verify_runtime_name(runtime)
    except Exception as e:
        return error(str(e))

    pull_runtime = standalone_config.get('pull_runtime', False)
    localhost_handler = LocalhostHandler({'runtime': runtime, 'pull_runtime': pull_runtime})
    runtime_meta = localhost_handler.create_runtime(runtime)
    response = flask.jsonify(runtime_meta)
    response.status_code = 200

    return response


def main():
    setup_lithops_logger('DEBUG', filename=LT_LOG_FILE)

    os.makedirs(LITHOPS_TEMP_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

    with open(LT_LOG_FILE, 'a') as log_file:
        sys.stdout = log_file
        sys.stderr = log_file
        init_keeper()
        server = WSGIServer(('0.0.0.0', LITHOPS_SERVICE_PORT), controller, log=controller.logger)
        server.serve_forever()


if __name__ == '__main__':
    main()
