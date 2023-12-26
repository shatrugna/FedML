import json
import logging
import os
import time
import traceback
from urllib.parse import urlparse

import asyncio

from fedml import mlops
from fedml.computing.scheduler.comm_utils.constants import SchedulerConstants
from fedml.computing.scheduler.scheduler_core.compute_cache_manager import ComputeCacheManager
from fedml.computing.scheduler.slave import client_data_interface
from fedml.computing.scheduler.master import server_data_interface
from fedml.computing.scheduler.model_scheduler import device_client_data_interface
from fedml.computing.scheduler.model_scheduler import device_server_data_interface
from fedml.core.common.singleton import Singleton

from .container_utils import ContainerUtils
from .job_utils import JobRunnerUtils
from ..model_scheduler.device_http_proxy_inference_protocol import FedMLHttpProxyInference
from ..model_scheduler.device_model_cache import FedMLModelCache
from ..model_scheduler.device_mqtt_inference_protocol import FedMLMqttInference
from ..slave import client_constants
from ..master import server_constants
from ..model_scheduler import device_client_constants
from ..model_scheduler import device_server_constants
from fedml.computing.scheduler.model_scheduler.device_http_inference_protocol import FedMLHttpInference
from fedml.core.mlops.mlops_runtime_log import MLOpsRuntimeLog
from fedml.core.mlops.mlops_runtime_log_daemon import MLOpsRuntimeLogDaemon


class JobMonitor(Singleton):
    ENDPOINT_CONTAINER_LOG_PREFIX = "endpoint"
    TIME_INTERVAL_FOR_INFERENCE_ON_GATEWAY = 60 * 10

    def __init__(self):
        if not hasattr(self, "endpoint_unavailable_counter"):
            self.endpoint_unavailable_counter = dict()
        if not hasattr(self, "released_endpoints"):
            self.released_endpoints = dict()
        if not hasattr(self, "released_runs"):
            self.released_runs = dict()
        if not hasattr(self, "reported_runs"):
            self.reported_runs = dict()
        if not hasattr(self, "reported_runs_on_edges"):
            self.reported_runs_on_edges = dict()
        if not hasattr(self, "mqtt_config"):
            self.mqtt_config = dict()
        if not hasattr(self, "is_first_inference_on_gateway"):
            self.is_first_inference_on_gateway = True
        if not hasattr(self, "last_time_on_inference_gateway"):
            self.last_time_on_inference_gateway = None

    @staticmethod
    def get_instance():
        return JobMonitor()

    def monitor_slave_run_process_status(self):
        try:
            ComputeCacheManager.get_instance().set_redis_params()
            count = 0
            job_list = client_data_interface.FedMLClientDataInterface.get_instance().get_jobs_from_db()
            for job in job_list.job_list:
                count += 1
                if count >= 1000:
                    break

                # Calc the timeout
                started_time = int(float(job.started_time))
                timeout = time.time() - started_time

                # Check if all processes of the specific run are exited
                run_process_list = client_constants.ClientConstants.get_learning_process_list(job.job_id)
                all_run_processes_exited = True if len(run_process_list) <= 0 else False
                if all_run_processes_exited:
                    if not JobRunnerUtils.get_instance().released_runs.get(str(job.job_id), False):
                        JobRunnerUtils.get_instance().released_runs[str(job.job_id)] = True
                        # Release the gpu ids
                        JobRunnerUtils.get_instance().release_gpu_ids(job.job_id, job.edge_id)

                # Get the timeout threshold
                timeout_threshold = None
                if job.status == client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_PROVISIONING or \
                        job.status == client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_QUEUED:
                    timeout_threshold = SchedulerConstants.TRAIN_PROVISIONING_TIMEOUT
                elif job.status == client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_INITIALIZING or \
                        job.status == client_constants.ClientConstants.MSG_MLOPS_RUN_STATUS_STARTING or \
                        job.status == client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_UPGRADING:
                    timeout_threshold = SchedulerConstants.TRAIN_STARTING_TIMEOUT
                elif job.status == client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_TRAINING or \
                        job.status == client_constants.ClientConstants.MSG_MLOPS_RUN_STATUS_RUNNING:
                    timeout_threshold = SchedulerConstants.TRAIN_RUNNING_TIMEOUT
                elif job.status == client_constants.ClientConstants.MSG_MLOPS_RUN_STATUS_STOPPING:
                    timeout_threshold = SchedulerConstants.TRAIN_STOPPING_TIMEOUT

                # If the run processes have exited but run status is not completed and
                # timeout is out of the range, then release gpu ids and report failed status to the master agent.
                if all_run_processes_exited and not SchedulerConstants.is_run_completed(job.status) and \
                        timeout_threshold is not None and timeout > timeout_threshold:
                    # Report failed status to the master agent
                    mlops.log_training_failed_status(
                        run_id=job.job_id, edge_id=job.edge_id, enable_broadcast=True)

                    print(f"[Slave][{job.job_id}:{job.edge_id}] Due to timeout, release gpu ids and "
                          f"set run status of slave to failed.")

        except Exception as e:
            logging.info(f"Exception when monitoring run process on the slave agent.{traceback.format_exc()}")
            pass

        try:
            ComputeCacheManager.get_instance().set_redis_params()
            count = 0
            FedMLModelCache.get_instance().set_redis_params()
            try:
                device_client_data_interface.FedMLClientDataInterface.get_instance().create_job_table()
            except Exception as e:
                pass
            job_list = device_client_data_interface.FedMLClientDataInterface.get_instance().get_jobs_from_db()
            for job in job_list.job_list:
                count += 1
                if count >= 1000:
                    break

                endpoint_status = FedMLModelCache.get_instance().get_end_point_status(job.job_id)

                if job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED or \
                        job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_KILLED or \
                        job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE or \
                        job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_IDLE or \
                        endpoint_status == device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_FAILED:
                    if not self.released_endpoints.get(str(job.job_id), False):
                        self.released_endpoints[str(job.job_id)] = True

                        # Release the gpu ids
                        JobRunnerUtils.get_instance().release_gpu_ids(job.job_id, job.edge_id)
                        print(f"[Run/EndpointWorker][{job.job_id}:{job.edge_id}] Release gpu ids.")

        except Exception as e:
            logging.info(f"Exception when monitoring endpoint process on the slave agent.{traceback.format_exc()}")
            pass

    def monitor_master_run_process_status(self, server_id, device_info_reporter=None):
        try:
            ComputeCacheManager.get_instance().set_redis_params()
            count = 0
            job_list = server_data_interface.FedMLServerDataInterface.get_instance().get_jobs_from_db()
            for job in job_list.job_list:
                count += 1
                if count >= 1000:
                    break

                # Calc the timeout
                started_time = int(float(job.started_time))
                timeout = time.time() - started_time

                # Get the timeout threshold
                timeout_threshold = None
                if job.status == server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_PROVISIONING:
                    timeout_threshold = SchedulerConstants.TRAIN_PROVISIONING_TIMEOUT
                elif job.status == server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_STARTING or \
                        job.status == server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_UPGRADING:
                    timeout_threshold = SchedulerConstants.TRAIN_STARTING_TIMEOUT
                elif job.status == server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_RUNNING:
                    timeout_threshold = SchedulerConstants.TRAIN_RUNNING_TIMEOUT
                elif job.status == server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_STOPPING:
                    timeout_threshold = SchedulerConstants.TRAIN_STOPPING_TIMEOUT

                # Check if the timout is greater than the threshold value
                if timeout_threshold is not None and timeout > timeout_threshold:
                    if not self.reported_runs.get(str(job.job_id), False):
                        self.reported_runs[str(job.job_id)] = True

                        # Report failed status to the master agent
                        mlops.log_aggregation_failed_status(run_id=job.job_id, edge_id=server_id)
                        print(
                            f"[Master][{job.job_id}:{job.edge_id}:{server_id}] Due to timeout, set run status to failed.")

                # Request all running process list from the edge device.
                running_timeout = timeout_threshold if timeout_threshold is not None else \
                    SchedulerConstants.TRAIN_INIT_TIMEOUT
                if not SchedulerConstants.is_run_completed(job.status) and \
                        timeout > running_timeout:
                    run_completed_on_all_edges = True
                    job_run_json = json.loads(job.running_json)
                    edge_ids = job_run_json.get("edgeids", [])
                    for edge_id in edge_ids:
                        device_info = device_info_reporter.get_device_info(job.job_id, edge_id, server_id)
                        if device_info is None:
                            continue
                        run_process_list_map = device_info.get("run_process_list_map", {})
                        run_process_list = run_process_list_map.get(str(job.job_id), [])
                        if len(run_process_list) <= 0:
                            # Report failed status to the master agent
                            if self.reported_runs_on_edges.get(str(job.job_id)) is None:
                                self.reported_runs_on_edges[str(job.job_id)] = dict()
                            if not self.reported_runs_on_edges[str(job.job_id)].get(str(edge_id), False):
                                self.reported_runs_on_edges[str(job.job_id)][str(edge_id)] = True

                                mlops.log_training_failed_status(run_id=job.job_id, edge_id=edge_id)
                                mlops.log_run_log_lines(
                                    job.job_id, edge_id, ["ERROR: Client process exited------------------------------"],
                                    SchedulerConstants.JOB_TASK_TYPE_TRAIN)
                                print(f"[Master][{job.job_id}:{edge_id}] Due to job terminated on the slave agent, "
                                      f"set run status of slave to failed.")
                        else:
                            run_completed_on_all_edges = False

                    # If run completed on all edges, then report run status to the master agent.
                    if run_completed_on_all_edges:
                        # Report failed status to the master agent
                        if not self.reported_runs.get(str(job.job_id), False):
                            self.reported_runs[str(job.job_id)] = True

                            mlops.log_run_log_lines(
                                job.job_id, job.edge_id, ["ERROR: Run failed ------------------------------"],
                                SchedulerConstants.JOB_TASK_TYPE_TRAIN)
                            mlops.log_aggregation_failed_status(run_id=job.job_id, edge_id=server_id)
                            print(f"[Master][{job.job_id}:{job.edge_id}:{server_id}] "
                                  f"Due to job failed on all slave agents, set run status to failed.")

        except Exception as e:
            logging.info(f"Exception when monitoring run process on the master agent.{traceback.format_exc()}")
            pass

    def monitor_slave_endpoint_status(self):
        try:
            ComputeCacheManager.get_instance().set_redis_params()
            count = 0
            FedMLModelCache.get_instance().set_redis_params()
            try:
                device_client_data_interface.FedMLClientDataInterface.get_instance().create_job_table()
            except Exception as e:
                pass
            job_list = device_client_data_interface.FedMLClientDataInterface.get_instance().get_jobs_from_db()
            for job in job_list.job_list:
                count += 1
                if count >= 1000:
                    break

                endpoint_status = FedMLModelCache.get_instance().get_end_point_status(job.job_id)
                if job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED:
                    if not self.released_endpoints.get(str(job.job_id), False):
                        self.released_endpoints[str(job.job_id)] = True

                        # Release the gpu ids
                        JobRunnerUtils.get_instance().release_gpu_ids(job.job_id, job.edge_id)
                        print(f"[Worker][{job.job_id}:{job.edge_id}] Release gpu ids.")

                elif job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_FINISHED:
                    endpoint_json = json.loads(job.running_json)
                    model_config = endpoint_json.get("model_config", {})
                    model_name = model_config.get("model_name", None)
                    endpoint_name = endpoint_json.get("end_point_name", None)

                    if model_name is not None and endpoint_status is not None and \
                            endpoint_status != device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE:
                        # Get model deployment result
                        deployment_result, activated = \
                            FedMLModelCache.get_instance().get_deployment_result_with_device_id(
                                job.job_id, endpoint_name, model_name, job.edge_id)

                        # Check the endpoint status
                        self._check_and_reset_endpoint_status(
                            job.job_id, job.edge_id, deployment_result, only_check_inference_ready_status=True)

                elif job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_OFFLINE:
                    endpoint_json = json.loads(job.running_json)
                    model_config = endpoint_json.get("model_config", {})
                    model_name = model_config.get("model_name", None)
                    endpoint_name = endpoint_json.get("end_point_name", None)

                    deployment_result, activated = \
                        FedMLModelCache.get_instance().get_deployment_result_with_device_id(
                            job.job_id, endpoint_name, model_name, job.edge_id)
                    self._check_and_reset_endpoint_status(
                        job.job_id, job.edge_id, deployment_result, only_check_inference_ready_status=True)
                elif job.status == device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_RUNNING:
                    started_time = int(float(job.started_time))
                    timeout = time.time() - started_time
                    if timeout > SchedulerConstants.ENDPOINT_DEPLOYMENT_DEPLOYING_TIMEOUT:
                        print(f"[Worker][{job.job_id}:{job.edge_id}] Due to timeout, "
                              f"set worker status to status "
                              f"{device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_KILLED}.")

                        mlops.log_training_status(
                            device_client_constants.ClientConstants.MSG_MLOPS_CLIENT_STATUS_KILLED, run_id=job.job_id,
                            edge_id=job.edge_id, is_from_model=True, enable_broadcast=True
                        )

                        if not self.released_endpoints.get(str(job.job_id), False):
                            self.released_endpoints[str(job.job_id)] = True

                            # Release the gpu ids
                            JobRunnerUtils.get_instance().release_gpu_ids(job.job_id, job.edge_id)
                            print(f"[Worker][{job.job_id}:{job.edge_id}] Release gpu ids.")
        except Exception as e:
            logging.info(
                f"[Worker] Exception when syncing endpoint process on the slave agent. {traceback.format_exc()}")
            pass

    def _check_and_reset_endpoint_status(
            self, endpoint_id, device_id, deployment_result, only_check_inference_ready_status=False,
            should_release_gpu_ids=True
    ):
        result_json = deployment_result
        inference_url = result_json.get("model_url", None)
        model_metadata = result_json.get("model_metadata", {})
        input_list = model_metadata.get("inputs", None)
        output_list = []

        # Run inference request to check if endpoint is running normally.
        while True:
            if only_check_inference_ready_status:
                response_ok = self.is_inference_ready(
                    inference_url, timeout=SchedulerConstants.ENDPOINT_INFERENCE_READY_TIMEOUT,
                    device_id=device_id, endpoint_id=endpoint_id
                )
            else:
                response_ok, inference_response = self.inference(
                    device_id, endpoint_id, inference_url, input_list, output_list,
                    timeout=SchedulerConstants.ENDPOINT_STATUS_CHECK_TIMEOUT
                )

            if self.endpoint_unavailable_counter.get(str(endpoint_id)) is None:
                self.endpoint_unavailable_counter[str(endpoint_id)] = 0
            if not response_ok:
                self.endpoint_unavailable_counter[str(endpoint_id)] += 1
            else:
                self.endpoint_unavailable_counter[str(endpoint_id)] = 0
                return True

            # If the endpoint unavailable counter is greater than the threshold value,
            # then release gpu ids and report failed status to the master agent.
            if self.endpoint_unavailable_counter.get(str(endpoint_id), 0) > \
                    SchedulerConstants.ENDPOINT_FAIL_THRESHOLD_VALUE:
                if not self.released_endpoints.get(str(endpoint_id), False) and should_release_gpu_ids:
                    self.released_endpoints[str(endpoint_id)] = True

                    # Release the gpu ids
                    JobRunnerUtils.get_instance().release_gpu_ids(endpoint_id, device_id)
                return False
            time.sleep(2)

    def is_inference_ready(self, inference_url, timeout=None, device_id=None, endpoint_id=None):
        response_ok = asyncio.run(FedMLHttpInference.is_inference_ready(inference_url, timeout=timeout))
        if response_ok:
            print("Use http health check.")
            return response_ok
        print("Use http health check failed.")

        response_ok = asyncio.run(FedMLHttpProxyInference.is_inference_ready(
            inference_url, timeout=timeout))
        if response_ok:
            print("Use http proxy health check.")
            return response_ok
        print("Use http proxy health check failed.")

        agent_config = dict()
        agent_config["mqtt_config"] = dict()
        agent_config["mqtt_config"]["BROKER_HOST"] = self.mqtt_config["BROKER_HOST"]
        agent_config["mqtt_config"]["BROKER_PORT"] = self.mqtt_config["BROKER_PORT"]
        agent_config["mqtt_config"]["MQTT_USER"] = self.mqtt_config["MQTT_USER"]
        agent_config["mqtt_config"]["MQTT_PWD"] = self.mqtt_config["MQTT_PWD"]
        agent_config["mqtt_config"]["MQTT_KEEPALIVE"] = self.mqtt_config["MQTT_KEEPALIVE"]
        mqtt_inference = FedMLMqttInference(agent_config=agent_config, run_id=endpoint_id)
        response_ok = mqtt_inference.run_mqtt_health_check_with_request(
            device_id, endpoint_id, inference_url, timeout=timeout)

        print(f"Use mqtt health check. return {response_ok}")
        return response_ok

    def inference(
            self, device_id, endpoint_id, inference_url, input_list, output_list,
            inference_type="default", timeout=None):
        try:
            response_ok = asyncio.run(FedMLHttpInference.is_inference_ready(inference_url))
            if response_ok:
                response_ok, inference_response = asyncio.run(FedMLHttpInference.run_http_inference_with_curl_request(
                    inference_url, input_list, output_list, inference_type=inference_type, timeout=timeout))
                print(f"Use http inference. return {response_ok}.")
                return response_ok, inference_response

            response_ok = asyncio.run(FedMLHttpProxyInference.is_inference_ready(inference_url))
            if response_ok:
                response_ok, inference_response = asyncio.run(
                    FedMLHttpProxyInference.run_http_proxy_inference_with_request(
                        endpoint_id, inference_url, input_list, output_list, inference_type=inference_type,
                        timeout=timeout))
                print(f"Use http proxy inference. return {response_ok}.")
                return response_ok, inference_response

            agent_config = dict()
            agent_config["mqtt_config"] = dict()
            agent_config["mqtt_config"]["BROKER_HOST"] = self.mqtt_config["BROKER_HOST"]
            agent_config["mqtt_config"]["BROKER_PORT"] = self.mqtt_config["BROKER_PORT"]
            agent_config["mqtt_config"]["MQTT_USER"] = self.mqtt_config["MQTT_USER"]
            agent_config["mqtt_config"]["MQTT_PWD"] = self.mqtt_config["MQTT_PWD"]
            agent_config["mqtt_config"]["MQTT_KEEPALIVE"] = self.mqtt_config["MQTT_KEEPALIVE"]
            mqtt_inference = FedMLMqttInference(agent_config=agent_config, run_id=endpoint_id)
            response_ok = mqtt_inference.run_mqtt_health_check_with_request(
                device_id, endpoint_id, inference_url, timeout=timeout)
            if response_ok:
                response_ok, inference_response = mqtt_inference.run_mqtt_inference_with_request(
                    device_id, endpoint_id, inference_url, input_list, output_list, inference_type=inference_type,
                    timeout=timeout)

            if not response_ok:
                inference_response = {"error": True,
                                      "message": "Failed to use http, http-proxy and mqtt for inference."}

            print(f"Use mqtt inference. return {response_ok}.")
            return response_ok, inference_response
        except Exception as e:
            inference_response = {"error": True,
                                  "message": f"Exception when using http, http-proxy and mqtt for inference: {traceback.format_exc()}."}
            logging.info("Inference Exception: {}".format(traceback.format_exc()))
            return False, inference_response

        return False, None

    def _check_all_slave_endpoint_status(self, endpoint_id, endpoint_name, model_name):
        # Get model deployment result
        is_endpoint_offline = True
        gateway_device_id = None
        gateway_result_payload_for_ready = None
        result_list = FedMLModelCache.get_instance().get_deployment_result_list(
            endpoint_id, endpoint_name, model_name)
        for result_item in result_list:
            result_device_id, result_payload = FedMLModelCache.get_instance().get_result_item_info(
                result_item)

            # Check if the endpoint is activated
            endpoint_activated = FedMLModelCache.get_instance().get_end_point_activation(endpoint_id)
            if not endpoint_activated:
                print(f"[Master][{endpoint_id}] Endpoint is not activated, set status to "
                      f"offline after deployed")
                mlops.log_endpoint_status(
                    endpoint_id,
                    device_server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_OFFLINE)
                FedMLModelCache.get_instance().set_end_point_status(
                    endpoint_id, endpoint_name,
                    device_server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_OFFLINE)
                break
            else:
                # Check if the endpoint is running
                model_url = result_payload.get("model_url", "")
                url_parsed = urlparse(model_url)
                if url_parsed.path.startswith("/inference"):
                    gateway_device_id = result_device_id
                    gateway_result_payload_for_ready = result_payload
                    url_parsed = urlparse(result_payload.get("model_url", ""))
                    gateway_result_payload_for_ready[
                        "model_url"] = f"http://localhost:{url_parsed.port}{url_parsed.path}"
                else:
                    if self._check_and_reset_endpoint_status(
                            endpoint_id, result_device_id, result_payload, only_check_inference_ready_status=True,
                            should_release_gpu_ids=False):
                        is_endpoint_offline = False

        if gateway_result_payload_for_ready is not None and is_endpoint_offline is False:
            if not self._check_and_reset_endpoint_status(
                    endpoint_id, gateway_device_id, gateway_result_payload_for_ready,
                    only_check_inference_ready_status=True, should_release_gpu_ids=False):
                is_endpoint_offline = True

        return not is_endpoint_offline

    def monitor_master_endpoint_status(self):
        try:
            ComputeCacheManager.get_instance().set_redis_params()
            count = 0
            FedMLModelCache.get_instance().set_redis_params()
            try:
                device_server_data_interface.FedMLServerDataInterface.get_instance().create_job_table()
            except Exception as e:
                pass
            job_list = device_server_data_interface.FedMLServerDataInterface.get_instance().get_jobs_from_db()
            for job in job_list.job_list:
                count += 1
                if count >= 1000:
                    break

                endpoint_status = FedMLModelCache.get_instance().get_end_point_status(job.job_id)
                endpoint_json = json.loads(job.running_json) if job.running_json is not None else {}
                model_config = endpoint_json.get("model_config", {})
                model_name = model_config.get("model_name", None)
                endpoint_name = endpoint_json.get("end_point_name", None)

                if endpoint_status == device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_FAILED:
                    # Release the gpu ids
                    JobRunnerUtils.get_instance().release_gpu_ids(job.job_id, job.edge_id)
                elif endpoint_status == device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_DEPLOYED:
                    if model_name is None:
                        continue
                    try:
                        # If the endpoint is offline, then report offline status to the MLOps.
                        is_endpoint_online = self._check_all_slave_endpoint_status(job.job_id, endpoint_name,
                                                                                   model_name)
                        if not is_endpoint_online:
                            print(f"[Master][{job.job_id}] Due to all worker is offline, set endpoint status to "
                                  f"offline after deployed .")
                            mlops.log_endpoint_status(
                                job.job_id,
                                device_server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_OFFLINE)
                            FedMLModelCache.get_instance().set_end_point_status(
                                job.job_id, endpoint_name,
                                device_server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_OFFLINE)

                    except Exception as e:
                        print(f"[Master][{job.job_id}] Exception when check endpoint status: "
                              f"{traceback.format_exc()}")
                elif endpoint_status == \
                        device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_INITIALIZING:
                    started_time = int(float(job.started_time))
                    timeout = time.time() - started_time
                    if timeout > SchedulerConstants.ENDPOINT_DEPLOYMENT_PROVISIONING_TIMEOUT:
                        print(f"[Master][{job.job_id}] Due to timeout, set endpoint status to "
                              f"offline at the status {endpoint_status}.")
                        mlops.log_endpoint_status(
                            job.job_id,
                            device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_KILLED)
                        FedMLModelCache.get_instance().set_end_point_status(
                            job.job_id, endpoint_name,
                            device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_KILLED)
                elif endpoint_status == \
                        device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_DEPLOYING:
                    started_time = int(float(job.started_time))
                    timeout = time.time() - started_time
                    if timeout > SchedulerConstants.ENDPOINT_DEPLOYMENT_DEPLOYING_TIMEOUT:
                        print(f"[Master][{job.job_id}] Due to timeout, set endpoint status to "
                              f"offline at the status {endpoint_status}.")
                        mlops.log_endpoint_status(
                            job.job_id,
                            device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_KILLED)
                        FedMLModelCache.get_instance().set_end_point_status(
                            job.job_id, endpoint_name,
                            device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_KILLED)
                elif endpoint_status == device_server_constants.ServerConstants.MSG_MLOPS_SERVER_STATUS_OFFLINE:
                    # If the endpoint is offline, then report offline status to the MLOps.
                    is_endpoint_online = self._check_all_slave_endpoint_status(job.job_id, endpoint_name, model_name)
                    if is_endpoint_online:
                        print(f"[Master][{job.job_id}] Due to all worker is from offline to online, "
                              f"set endpoint status to online.")
                        mlops.log_endpoint_status(
                            job.job_id,
                            device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_DEPLOYED)
                        FedMLModelCache.get_instance().set_end_point_status(
                            job.job_id, endpoint_name,
                            device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_DEPLOYED)

        except Exception as e:
            logging.info(f"Exception when syncing endpoint process on the master agent {traceback.format_exc()}.")
            pass

    def monitor_endpoint_logs(self):
        try:
            fedml_args = mlops.get_fedml_args()
            ComputeCacheManager.get_instance().set_redis_params()
            count = 0
            FedMLModelCache.get_instance().set_redis_params()
            try:
                device_server_data_interface.FedMLServerDataInterface.get_instance().create_job_table()
            except Exception as e:
                pass
            job_list = device_server_data_interface.FedMLServerDataInterface.get_instance().get_jobs_from_db()
            for job in job_list.job_list:
                count += 1
                if count >= 1000:
                    break

                endpoint_status = FedMLModelCache.get_instance().get_end_point_status(job.job_id)
                if endpoint_status != device_server_constants.ServerConstants.MSG_MODELOPS_DEPLOYMENT_STATUS_DEPLOYED:
                    continue

                endpoint_json = json.loads(job.running_json) if job.running_json is not None else {}
                model_config = endpoint_json.get("model_config", {})
                model_name = model_config.get("model_name", None)
                model_id = model_config.get("model_id", None)
                model_version = model_config.get("model_version", None)
                endpoint_name = endpoint_json.get("end_point_name", None)

                log_file_path, program_prefix = MLOpsRuntimeLog.build_log_file_path_with_run_params(
                    job.job_id, job.edge_id, device_server_constants.ServerConstants.get_log_file_dir(), is_server=True,
                    log_file_prefix=JobMonitor.ENDPOINT_CONTAINER_LOG_PREFIX,
                )
                if not MLOpsRuntimeLogDaemon.get_instance(fedml_args).is_log_processor_running(job.job_id, job.edge_id):
                    setattr(fedml_args, "log_file_dir", os.path.dirname(log_file_path))
                    MLOpsRuntimeLogDaemon.get_instance(fedml_args).log_file_dir = os.path.dirname(log_file_path)
                    MLOpsRuntimeLogDaemon.get_instance(fedml_args).start_log_processor(
                        job.job_id, job.edge_id,
                        log_source=device_client_constants.ClientConstants.FEDML_LOG_SOURCE_TYPE_MODEL_END_POINT,
                        log_file_prefix=JobMonitor.ENDPOINT_CONTAINER_LOG_PREFIX
                    )

                # Get endpoint container name
                endpoint_container_name_prefix = device_client_constants.ClientConstants.get_endpoint_container_name(
                    endpoint_name, model_name, model_version, job.job_id, model_id
                )

                # Could be multiple containers for the same endpoint
                num_containers = ContainerUtils.get_instance().get_container_rank_same_model(
                    endpoint_container_name_prefix)

                for i in range(num_containers):
                    endpoint_container_name = endpoint_container_name_prefix + f"__{i}"

                    # Get endpoint logs from the container
                    endpoint_logs = ContainerUtils.get_instance().get_container_logs(endpoint_container_name)
                    
                    # Write container logs to the log file
                    if i == 0:
                        with open(log_file_path, "w") as f:
                            f.write(endpoint_logs)
                    else:
                        with open(log_file_path, "a") as f:
                            f.write(endpoint_logs)

        except Exception as e:
            logging.info(f"Exception when syncing endpoint log to MLOps {traceback.format_exc()}.")
