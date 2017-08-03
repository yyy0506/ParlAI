# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

from parlai.core.agents import Agent
from parlai.core.worlds import display_messages

import os
import time
from datetime import datetime
import random
import string
import json
from parlai.core.agents import create_agent_from_shared
from parlai.mturk.core.server_utils import setup_server, create_hit_config
from parlai.mturk.core.mturk_utils import calculate_mturk_cost, check_mturk_balance, create_hit_type, create_hit_with_hit_type, get_mturk_client, setup_aws_credentials
import threading
from parlai.mturk.core.data_model import COMMAND_SEND_MESSAGE, COMMAND_SHOW_DONE_BUTTON, COMMAND_EXPIRE_HIT, COMMAND_SUBMIT_HIT, COMMAND_CHANGE_CONVERSATION
from botocore.exceptions import ClientError
import uuid
from socketIO_client_nexus import SocketIO
import webbrowser
import requests
import logging

ASSIGNMENT_NOT_DONE = 'NotDone'
ASSIGNMENT_DONE = 'Submitted'
ASSIGNMENT_APPROVED = 'Approved'
ASSIGNMENT_REJECTED = 'Rejected'

TIMEOUT_MESSAGE = '[TIMEOUT]'

logging_enabled = True
logger = None
debug = True

if logging_enabled:
    logging.basicConfig(filename=str(time.time())+'.log',
                        filemode='w',
                        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S',
                        level=logging.DEBUG)
    logger = logging.getLogger('mturk')

def print_and_log(message, should_print=True):
    if logging_enabled:
        logger.info(message)
    if should_print or debug: # Always print message in debug mode
        print(message)

class MTurkManager():
    def __init__(self, opt, mturk_agent_ids):
        self.opt = opt
        self.server_url = None
        self.task_group_id = None
        self.socket_listen_thread = None
        self.socketIO = None
        self.run_id = None
        self.mturk_agent_ids = mturk_agent_ids
        self.hit_id_list = []
        self.task_files_to_copy = None
        self.is_sandbox = opt['is_sandbox']
        self.worker_pool = []
        self.change_condition = threading.Condition()
        self.worker_index = 0
        self.worker_candidates = None
        self.worker_id_to_onboard_thread = {}
        self.onboard_function = None
        self.task_threads = []
        self.conversation_index = 0
        self.num_completed_conversations = 0

    def init_aws(self, task_directory_path=None):
        print_and_log("\nYou are going to allow workers from Amazon Mechanical Turk to be an agent in ParlAI.\nDuring this process, Internet connection is required, and you should turn off your computer's auto-sleep feature.\n")
        key_input = input("Please press Enter to continue... ")
        print_and_log("")

        setup_aws_credentials()

        payment_opt = {
            'type': 'reward',
            'num_total_assignments': self.opt['num_conversations'] * len(self.mturk_agent_ids),
            'reward': self.opt['reward']  # in dollars
        }
        total_cost = calculate_mturk_cost(payment_opt=payment_opt)
        if not check_mturk_balance(balance_needed=total_cost, is_sandbox=self.opt['is_sandbox']):
            return

        print_and_log('Setting up MTurk server...')
        create_hit_config(task_description=self.opt['task_description'], is_sandbox=self.opt['is_sandbox'])
        if not self.task_files_to_copy:
            self.task_files_to_copy = []
        if not task_directory_path:
            task_directory_path = os.path.join(self.opt['parlai_home'], 'parlai', 'mturk', 'tasks', self.opt['task'])
        self.task_files_to_copy.append(os.path.join(task_directory_path, 'html', 'cover_page.html'))
        for mturk_agent_id in self.mturk_agent_ids:
            self.task_files_to_copy.append(os.path.join(task_directory_path, 'html', mturk_agent_id+'_index.html'))
        self.server_url, db_host = setup_server(task_files_to_copy = self.task_files_to_copy)
        print_and_log(self.server_url, False)

        print_and_log('RDS: Cleaning database...')
        params = {
            'db_host': db_host
        }
        response = requests.get(self.server_url+'/clean_database', params=params)
        assert(response.status_code != '200')

        print_and_log('Local: Setting up SocketIO...')

        print_and_log("MTurk server setup done.\n")

    def ready_to_accept_workers(self):
        self.setup_socket(mturk_server_url=self.server_url, port=443)

    def start_new_run(self):
        self.run_id = str(int(time.time()))
        self.task_group_id = str(self.opt['task']) + '_' + str(self.run_id)

    def set_onboard_function(self, onboard_function):
        self.onboard_function = onboard_function

    def onboard_new_worker(self, mturk_agent):
        def _onboard_function(mturk_agent):
            if self.onboard_function:
                self.onboard_function(mturk_agent)
            with self.change_condition:
                print("Adding worker to pool...")
                self.worker_pool.append(mturk_agent)
                self.change_condition.notify()

        if not mturk_agent.worker_id in self.worker_id_to_onboard_thread:
            mturk_agent.change_conversation(conversation_id='o_'+str(uuid.uuid4()), agent_id='Worker')
            onboard_thread = threading.Thread(target=_onboard_function, args=(mturk_agent,))
            onboard_thread.daemon = True
            onboard_thread.start()
            self.worker_id_to_onboard_thread[mturk_agent.worker_id] = onboard_thread

    def start_task(self, eligibility_function, role_function, task_function):
        def _task_function():
            print("Starting task...")
            num_abandoned_agents = 0
            if task_function:
                num_abandoned_agents = task_function()
            if num_abandoned_agents == 0:
                with self.change_condition:
                    self.num_completed_conversations += 1
                    self.change_condition.notify()
            else:
                # Create more HITs in order to match the total number of valid conversations
                self.create_additional_hits(num_hits=len(self.mturk_agent_ids)-num_abandoned_agents, unique_worker=self.opt['unique_worker'])

        while self.num_completed_conversations < self.opt['num_conversations']:
            print("here5")
            with self.change_condition:
                self.worker_candidates = []
                for worker in self.worker_pool:
                    if eligibility_function(worker): # Decides whether this worker can be in a conversation
                        self.worker_candidates.append(worker)
                        if len(self.worker_candidates) == len(self.mturk_agent_ids):
                            break
                if len(self.worker_candidates) == len(self.mturk_agent_ids): # Spawn a TaskWorld and put these workers in a new conversation
                    self.conversation_index += 1
                    new_conversation_id = 't_' + str(self.conversation_index)
                    for worker in self.worker_candidates:
                        self.worker_pool.remove(worker)
                        worker_agent_id = role_function(worker)
                        worker.change_conversation(conversation_id=new_conversation_id, agent_id=worker_agent_id)
                        
                    self.worker_candidates.sort(key=lambda x: self.mturk_agent_ids.index(x.id))

                    task_thread = threading.Thread(target=_task_function, args=(self.opt, self.worker_candidates))
                    task_thread.daemon = True
                    task_thread.start()
                    self.task_threads.append(task_thread)
                self.change_condition.wait()

    def _send_event_to_socket(self, event_name, event_data, response_handler=None):
        event_sent = threading.Event()
        def on_event_sent(*args):
            if not event_sent.is_set():
                event_sent.set()
        emit_success = False
        while not emit_success:
            try:
                print_and_log(event_name + ' sending to server. Data: ' + str(event_data), False)
                self.socketIO.emit(event_name, event_data, on_event_sent)
                emit_success = True
            except Exception as e:
                print_and_log(e, False)
                emit_success = False

        def check_event_sent():
            if event_sent.wait(timeout=1): # Timeout in seconds
                print_and_log(event_name + ' is acknowledged by server.', False)
                if response_handler:
                    response_handler()
            else:
                print_and_log(event_name + ' not acknowledged by server. Sending event again.', False)
                self._send_event_to_socket(event_name, event_data, response_handler)

        thread = threading.Thread(target=check_event_sent)
        thread.daemon = True
        thread.start()

    def setup_socket(self, mturk_server_url, port):
        self.socketIO = SocketIO(mturk_server_url, port)

        def on_socket_open(*args):
            print_and_log("on_socket_open: " + str(args), False)
            self._send_event_to_socket(
                'agent_alive', 
                {
                    'task_group_id': self.task_group_id,
                    'assignment_id': None,
                    'hit_id': None,
                    'worker_id': '[World]'
                }
            )

        def on_disconnect(*args):
            print_and_log("Server disconnected: " + str(args), False)

        def on_agent_alive(*args):
            print_and_log("on_agent_alive: " + str(args), False)
            agent_info = args[0]
            task_group_id = agent_info['task_group_id']
            assignment_id = agent_info['assignment_id']
            hit_id = agent_info['hit_id']
            worker_id = agent_info['worker_id']

            if task_group_id != self.task_group_id:
                self._send_event_to_socket('agent_alive_received', {'by_worker_id': '[World]'})
                return
        
            # Match MTurkAgent object with actual Turker
            mturk_agent, is_new_agent = MTurkAgent.get_agent(
                worker_id=worker_id,
                opt=self.opt,
                mturk_manager=self
            )
            self._send_event_to_socket(
                'agent_alive_received', 
                {
                    'task_group_id': task_group_id,
                    'conversation_id': mturk_agent.conversation_id,
                    'agent_id': mturk_agent.id,
                    'by_worker_id': '[World]'
                }
            )
            mturk_agent.assignment_id = assignment_id
            mturk_agent.hit_id = hit_id
            mturk_agent.worker_id = worker_id

            if is_new_agent:
                self.onboard_new_worker(mturk_agent=mturk_agent)

        def on_new_message(*args):
            print_and_log("on_new_message: " + str(args), False)

            message = args[0]

            self._send_event_to_socket(
                'new_message_received', 
                {
                    'by_worker_id': '[World]'
                }
            )

            if message['task_group_id'] != self.task_group_id:
                return

            MTurkAgent.set_new_message(
                worker_id=message['sender_worker_id'],
                new_message=message
            )

        self.socketIO.on('socket_open', on_socket_open)
        self.socketIO.on('agent_alive', on_agent_alive)
        self.socketIO.on('new_message', on_new_message)
        self.socketIO.on('disconnect', on_disconnect)

        self.socket_listen_thread = threading.Thread(target=self._socket_receive_events)
        self.socket_listen_thread.daemon = True
        self.socket_listen_thread.start()
        
    def _socket_receive_events(self):
        self.socketIO.wait()

    def send_command_to_agent(self, task_group_id, conversation_id, sender_agent_id, receiver_agent_id, receiver_worker_id, command, command_data=None):
        command_dict = {
            'task_group_id': task_group_id,
            'conversation_id': conversation_id,
            'sender_agent_id': sender_agent_id,
            'receiver_agent_id': receiver_agent_id,
            'receiver_worker_id': receiver_worker_id,
            'command': command,
            'command_id': str(uuid.uuid4()),
        }
        if command_data:
            command_dict['command_data'] = command_data

        def on_agent_send_command_response(*args):
            print_and_log("on_agent_send_command_response: "+str(args), False)

        self._send_event_to_socket(
            'agent_send_command',
            command_dict,
            on_agent_send_command_response
        )
        self.socketIO.wait_for_callbacks(seconds=0.1)

    def send_message_to_agent(self, task_group_id, conversation_id, sender_agent_id, receiver_agent_id, receiver_worker_id, message):
        timestamp = None
        response = requests.get(self.server_url+'/get_timestamp')
        try:
            ret = response.json()
            timestamp = ret['timestamp']
        except Exception as e:
            print(response.content)
            raise e

        message['task_group_id'] = task_group_id
        message['conversation_id'] = conversation_id
        message['sender_agent_id'] = sender_agent_id
        message['receiver_agent_id'] = receiver_agent_id
        message['receiver_worker_id'] = receiver_worker_id
        message['message_id'] = str(uuid.uuid4())
        message['timestamp'] = timestamp

        def on_agent_send_message_response(*args):
            print_and_log("on_agent_send_message_response: "+str(args), False)

        self._send_event_to_socket(
            'agent_send_message',
            message,
            on_agent_send_message_response
        )
        self.socketIO.wait_for_callbacks(seconds=0.1)

    def send_new_message(self, task_group_id, conversation_id, sender_agent_id, receiver_agent_id, receiver_worker_id, message_text=None, reward=None, episode_done=False):
        message = {
            'id': sender_agent_id,
            'episode_done': episode_done,
        }
        if message_text:
            message['text'] = message_text
        if reward:
            message['reward'] = reward

        self.send_message_to_agent(
            task_group_id=task_group_id,
            conversation_id=conversation_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            receiver_worker_id=receiver_worker_id,
            message=message
        )

    def send_new_command(self, task_group_id, conversation_id, sender_agent_id, receiver_agent_id, receiver_worker_id, command, command_data=None):
        self.send_command_to_agent(
            task_group_id=task_group_id,
            conversation_id=conversation_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            receiver_worker_id=receiver_worker_id,
            command=command,
            command_data=command_data
        )

    def get_agent_work_status(self, assignment_id):
        client = get_mturk_client(self.is_sandbox)
        try:
            response = client.get_assignment(AssignmentId=assignment_id)
            return response['Assignment']['AssignmentStatus']
        except ClientError as e:
            if 'This operation can be called with a status of: Reviewable,Approved,Rejected' in e.response['Error']['Message']:
                return ASSIGNMENT_NOT_DONE

    def create_additional_hits(self, num_hits, unique_worker):
        print_and_log('Creating '+str(num_hits)+' hits. Unique worker: '+str(unique_worker), False)
        hit_type_id = create_hit_type(
            hit_title=self.opt['hit_title'],
            hit_description=self.opt['hit_description'] + ' (ID: ' + self.task_group_id + ')',
            hit_keywords=self.opt['hit_keywords'],
            hit_reward=self.opt['reward'],
            assignment_duration_in_seconds=self.opt.get('assignment_duration_in_seconds', 30 * 60), # Set to 30 minutes by default
            is_sandbox=self.opt['is_sandbox']
        )
        mturk_chat_url = self.server_url + "/chat_index?task_group_id="+str(self.task_group_id)
        print_and_log(mturk_chat_url, False)
        mturk_page_url = None

        if unique_worker:
            mturk_page_url, hit_id = create_hit_with_hit_type(
                page_url=mturk_chat_url,
                hit_type_id=hit_type_id,
                num_assignments=num_hits,
                is_sandbox=self.is_sandbox
            )
            self.hit_id_list.append(hit_id)
        else:
            for i in range(num_hits):
                mturk_page_url, hit_id = create_hit_with_hit_type(
                    page_url=mturk_chat_url,
                    hit_type_id=hit_type_id,
                    num_assignments=1,
                    is_sandbox=self.is_sandbox
                )
                self.hit_id_list.append(hit_id)
        return mturk_page_url

    def create_hits(self):
        print_and_log('Creating HITs...')

        mturk_page_url = self.create_additional_hits(
            num_hits=self.opt['num_conversations'] * len(self.mturk_agent_ids),
            unique_worker=self.opt['unique_worker']
        )

        print_and_log("Link to HIT: " + mturk_page_url + "\n")
        print_and_log("Waiting for Turkers to respond... (Please don't close your laptop or put your computer into sleep or standby mode.)\n")
        if self.opt['is_sandbox']:
            webbrowser.open(mturk_page_url)
        return mturk_page_url

    def expire_hit(self, hit_id):
        client = get_mturk_client(self.is_sandbox)
        client.update_expiration_for_hit(HITId=hit_id, ExpireAt=datetime(2015, 1, 1)) # Update it to a time in the past, and the HIT will be immediately expired

    def expire_all_unassigned_hits(self):
        print_and_log("Expiring all unassigned HITs...")
        print_and_log(self.hit_id_list, False)
        for hit_id in self.hit_id_list:
            self.expire_hit(hit_id)

    def approve_work(self, assignment_id):
        client = get_mturk_client(self.is_sandbox)
        client.approve_assignment(AssignmentId=assignment_id)

    def reject_work(self, assignment_id, reason):
        client = get_mturk_client(self.is_sandbox)
        client.reject_assignment(AssignmentId=assignment_id, RequesterFeedback=reason)

    def block_worker(self, worker_id, reason):
        client = get_mturk_client(self.is_sandbox)
        client.create_worker_block(WorkerId=worker_id, Reason=reason)

    def pay_bonus(self, worker_id, bonus_amount, assignment_id, reason, unique_request_token):
        total_cost = calculate_mturk_cost(payment_opt={'type': 'bonus', 'amount': bonus_amount})
        if not check_mturk_balance(balance_needed=total_cost, is_sandbox=self.is_sandbox):
            print_and_log("Cannot pay bonus. Reason: Insufficient fund in your MTurk account.")
            return False

        client = get_mturk_client(self.is_sandbox)
        client.send_bonus(
            WorkerId=worker_id,
            BonusAmount=bonus_amount,
            AssignmentId=assignment_id,
            Reason=reason,
            UniqueRequestToken=unique_request_token # Could be useful in the future, for handling network errors
        )

        return True

    def email_worker(self, worker_id, subject, message_text):
        client = get_mturk_client(self.is_sandbox)
        response = client.notify_workers(
            Subject=subject,
            MessageText=message_text,
            WorkerIds=[worker_id]
        )
        if len(response['NotifyWorkersFailureStatuses']) > 0:
            return {'failure': response['NotifyWorkersFailureStatuses'][0]['NotifyWorkersFailureMessage']}
        else:
            return {'success': True}

    def shutdown(self):
        setup_aws_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server_utils.py')
        print_and_log("Remote database instance will accumulate cost over time (about $30/month for t2.medium instance). Please run `python "+setup_aws_file_path+" remove_rds` to remove RDS instance if you don't plan to use MTurk often.")


class MTurkAgent(Agent):
    worker_id_to_instance = {}
    mturk_agent_list = []
    mturk_agent_list_lock = threading.Lock()

    @classmethod
    def set_new_message(cls, worker_id, new_message):
        if worker_id in cls.worker_id_to_instance:
            agent = cls.worker_id_to_instance[worker_id]
            with agent.new_message_lock:
                agent.new_message = new_message

    @classmethod
    def get_agent(cls, worker_id, opt, mturk_manager):
        agent = None
        is_new_agent = False
        if not worker_id in cls.worker_id_to_instance:
            agent = MTurkAgent(manager=mturk_manager, opt=opt)
            agent.worker_id = worker_id
            cls.worker_id_to_instance[worker_id] = agent
            cls.mturk_agent_list.append(agent) 
            is_new_agent = True           
        else:
            agent = cls.worker_id_to_instance[worker_id]
            is_new_agent = False
        return agent, is_new_agent

    def __init__(self, manager, opt, shared=None):
        super().__init__(opt)

        self.conversation_id = None
        self.manager = manager
        self.id = None
        self.assignment_id = None
        self.hit_id = None
        self.worker_id = None
        self.hit_is_abandoned = False

        self.new_message = None
        self.new_message_lock = threading.Lock()

    def observe(self, msg):
        self.manager.send_new_message(
            task_group_id=self.manager.task_group_id,
            conversation_id=self.conversation_id,
            sender_agent_id=msg['id'],
            receiver_agent_id=self.id,
            receiver_worker_id=self.worker_id,
            message_text=msg.get('text', None),
            reward=msg.get('reward', None),
            episode_done=msg.get('episode_done', False),
        )

    def act(self, timeout=None): # Timeout in seconds, after which the HIT will be expired automatically
        self.manager.send_new_command(
            task_group_id=self.manager.task_group_id,
            conversation_id=self.conversation_id,
            sender_agent_id='[World]',
            receiver_agent_id=self.id,
            receiver_worker_id=self.worker_id,
            command=COMMAND_SEND_MESSAGE
        )

        if timeout:
            start_time = time.time()

        # Wait for agent's new message
        while not self.new_message:
            if timeout:
                current_time = time.time()
                if (current_time - start_time) > timeout:
                    self.set_hit_is_abandoned()
                    msg = {
                        'id': self.id,
                        'text': TIMEOUT_MESSAGE,
                        'episode_done': True
                    }
                    return msg
            time.sleep(0.1)

        with self.new_message_lock:
            new_message = self.new_message
            self.new_message = None
            return new_message

    def episode_done(self):
        return False

    def change_conversation(self, conversation_id, agent_id):
        print_and_log('Changing conversation...', False)
        self.conversation_id = conversation_id
        self.id = agent_id

        self.manager.send_new_command(
            task_group_id=self.manager.task_group_id,
            conversation_id=self.conversation_id,
            sender_agent_id='[World]',
            receiver_agent_id=self.id,
            receiver_worker_id=self.worker_id,
            command=COMMAND_CHANGE_CONVERSATION,
            command_data={
                'conversation_id': self.conversation_id,
                'agent_id': self.id
            }
        )

    def approve_work(self):
        if self.hit_is_abandoned:
            print_and_log('Conversation ID: ' + str(self.conversation_id) + ', Agent ID: ' + self.id + ' - HIT is abandoned and thus not available for review.')
        else:
            if self.manager.get_agent_work_status(assignment_id=self.assignment_id) == ASSIGNMENT_DONE:
                self.manager.approve_work(assignment_id=self.assignment_id)
                print_and_log('Conversation ID: ' + str(self.conversation_id) + ', Agent ID: ' + self.id + ' - HIT is approved.')
            else:
                print_and_log("Cannot approve HIT. Reason: Turker hasn't completed the HIT yet.")

    def reject_work(self, reason='unspecified'):
        if self.hit_is_abandoned:
            print_and_log('Conversation ID: ' + str(self.conversation_id) + ', Agent ID: ' + self.id + ' - HIT is abandoned and thus not available for review.')
        else:
            if self.manager.get_agent_work_status(assignment_id=self.assignment_id) == ASSIGNMENT_DONE:
                self.manager.reject_work(assignment_id=self.assignment_id, reason=reason)
                print_and_log('Conversation ID: ' + str(self.conversation_id) + ', Agent ID: ' + self.id + ' - HIT is rejected.')
            else:
                print_and_log("Cannot reject HIT. Reason: Turker hasn't completed the HIT yet.")

    def block_worker(self, reason='unspecified'):
        self.manager.block_worker(worker_id=self.worker_id, reason=reason)
        print_and_log("Blocked worker ID: " + str(self.worker_id) + ". Reason: " + reason)

    def pay_bonus(self, bonus_amount, reason='unspecified'):
        if self.hit_is_abandoned:
            print_and_log('Conversation ID: ' + str(self.conversation_id) + ', Agent ID: ' + self.id + ' - HIT is abandoned and thus not available for bonus.')
        else:
            if self.manager.get_agent_work_status(assignment_id=self.assignment_id) != ASSIGNMENT_NOT_DONE:
                unique_request_token = str(uuid.uuid4())
                if self.manager.pay_bonus(worker_id=self.worker_id, bonus_amount=bonus_amount, assignment_id=self.assignment_id, reason=reason, unique_request_token=unique_request_token):
                    print_and_log("Paid $" + str(bonus_amount) + " bonus to WorkerId: " + self.worker_id)
            else:
                print_and_log("Cannot pay bonus for HIT. Reason: Turker hasn't completed the HIT yet.")

    def email_worker(self, subject, message_text):
        response = self.manager.email_worker(worker_id=self.worker_id, subject=subject, message_text=message_text)
        if 'success' in response:
            print_and_log("Email sent to worker ID: "+str(self.worker_id)+": Subject: "+str(subject)+": Text: "+str(message_text))
            return True
        elif 'failure' in response:
            print_and_log("Unable to send email to worker ID: "+str(self.worker_id)+". Error: "+str(response['failure']))
            return False

    def set_hit_is_abandoned(self):
        if not self.hit_is_abandoned:
            self.hit_is_abandoned = True
            self.manager.send_new_command(
                task_group_id=self.manager.task_group_id,
                conversation_id=self.conversation_id,
                sender_agent_id='[World]',
                receiver_agent_id=self.id,
                receiver_worker_id=self.worker_id,
                command=COMMAND_EXPIRE_HIT
            )

    def wait_for_hit_completion(self, timeout=None): # Timeout in seconds, after which the HIT will be expired automatically
        if timeout:
            start_time = time.time()
        while self.manager.get_agent_work_status(assignment_id=self.assignment_id) != ASSIGNMENT_DONE:
            if timeout:
                current_time = time.time()
                if (current_time - start_time) > timeout:
                    print_and_log("Timed out waiting for Turker to complete the HIT.")
                    self.set_hit_is_abandoned()
                    return False
            print_and_log("Waiting for Turker to complete the HIT...", False)
            time.sleep(1)
        print_and_log('Conversation ID: ' + str(self.conversation_id) + ', Agent ID: ' + self.id + ' - HIT is done.')
        return True

    def shutdown(self, timeout=None, direct_submit=False): # Timeout in seconds, after which the HIT will be expired automatically
        command_to_send = COMMAND_SHOW_DONE_BUTTON
        if direct_submit:
            command_to_send = COMMAND_SUBMIT_HIT
        if not self.hit_is_abandoned:
            self.manager.send_new_command(
                task_group_id=self.manager.task_group_id,
                conversation_id=self.conversation_id,
                sender_agent_id='[World]',
                receiver_agent_id=self.id,
                receiver_worker_id=self.worker_id,
                command=command_to_send
            )
            return self.wait_for_hit_completion(timeout=timeout)
