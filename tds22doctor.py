#!/usr/bin/python3

import argparse
import json
import os
import random
import subprocess
import time
import requests
from datetime import datetime, timedelta

parser = argparse.ArgumentParser(description='TDS22 Doctor')
parser.add_argument('--do', default=False, type=bool)
parser.add_argument('--debug', default=False, type=bool)
parser.add_argument('--auth_file', default='/root/tds22doctor/doctor.json', type=str)
parser.add_argument('-u', '--url', default='https://api.testnet.solana.com', type=str)

args = parser.parse_args()
RPC_URL = args.url
AUTH_FILE = args.auth_file

do_process = args.do
debug = args.debug

MAX_STAKE_ACTIVE = 3999
RPC_TIMEOUT = 60
EPOCH_MINIMUM_PERC_PASSED = 1

SOLANA_BINARY = os.getenv('SOLANA_BINARY', 'solana')
# Cache file to save all solana.org validators list
file_vercel = '/tmp/tds22doctor_vercel_api_all.txt'
# List of validators to skip 100%, those who are in range of Approved, Rejected
# can later skip this list, if we have enought vote accounts/stake to delegate
# would also need to improve "skip rate checks" so we do not delegate to dead nodes
sfdp_identity_skip_list = []


def solana_deactivate_string(stake_pubkey):
    return "{} deactivate-stake {} --stake-authority {} --fee-payer {} --keypair {} -u {} --rpc-timeout={}".format(
        SOLANA_BINARY,
        stake_pubkey,
        AUTH_FILE,
        AUTH_FILE,
        AUTH_FILE,
        RPC_URL,
        RPC_TIMEOUT,
    )


def solana_delegate_string(stake_pubkey, vote_address):
    return "{} delegate-stake {} {} --keypair {} --fee-payer {} -u {} --rpc-timeout={}".format(
        SOLANA_BINARY,
        stake_pubkey,
        vote_address,
        AUTH_FILE,
        AUTH_FILE,
        RPC_URL,
        RPC_TIMEOUT,
    )


def write_to_file(filename, data):
    with open(filename, 'w') as f:
        f.write(data)


def file_not_older_than(filename, hours=24):
    if not os.path.exists(filename):
        return False

    file_modified_time = datetime.fromtimestamp(os.path.getmtime(filename))
    current_time = datetime.now()
    time_difference = current_time - file_modified_time
    return time_difference < timedelta(hours=hours)


def cache_remove(cache_key):
    cache_file = '/tmp/tds22doctor_' + cache_key + '.txt'
    os.remove(cache_file)


def make_http_request_cached(url, cache_key):
    try:
        cache_file = '/tmp/tds22doctor_' + cache_key + '.txt'

        if file_not_older_than(cache_file, hours=6):
            with open(cache_file, 'r') as openfile:
                # print('reading cache: '+cache_file)
                return json.load(openfile)

        response = requests.get(url)
        write_to_file(
            cache_file,
            json.dumps(response.json(), indent=4)
        )

        return response.json()
    except requests.RequestException as e:
        return None


def shell_command(command):
    output_command = subprocess.check_output(command, shell=True, timeout=30)
    return output_command.decode("utf-8").rstrip()


def run_command_with_retry(command, max_retries=3):
    for _ in range(max_retries):
        try:
            # Run the command and capture both output and error
            result = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Decode the output and error messages
            output = result.stdout.decode("utf-8").rstrip()
            error = result.stderr.decode("utf-8").rstrip()

            # Retrieve the exit code
            exit_code = result.returncode

            if exit_code == 0:
                return output, error, exit_code
            else:
                print(f"FAILED. '{command}' failed with exit code {exit_code}. Retrying...")
                time.sleep(10)

        except Exception as e:
            return None, str(e), -1  # Return None for output if there's an error

    # If all retries failed
    return None, "Max retries reached", -1


def vercel_api_download_all():
    print('Downloading https://api.solana.org/api/validators/list...')
    # Clear file
    file1 = open(file_vercel, "w")
    file1.writelines([])
    file1.close()

    pending_pages = True
    page = 0
    limit = 100

    file1 = open(file_vercel, "a")  # append mode
    while pending_pages:
        offset = page * limit
        cache_key = 'validators_list_all_limit_{}_offset_{}'.format(limit, offset)

        url = "https://api.solana.org/api/validators/list?search_term=&offset={}&limit={}&order_by=name&order=asc".format(
            offset,
            limit
        )

        debug and print(url)
        vercel_api = make_http_request_cached(
            url,
            cache_key
        )

        # Probably rate limited?
        if 'data' not in vercel_api:
            print(vercel_api)
            # We invalidate cache, because response would be cached with invalid data.
            cache_remove(cache_key)
            time.sleep(30)
            continue

        if not vercel_api['data']:
            pending_pages = False

            debug and print('Last page fetched: {}'.format(url))
            print('Finished fetching.')
            continue

        debug and print('Total results from page: {} results count: {}.'.format(page, len(vercel_api['data'])))

        for row in vercel_api['data']:
            file1.write(json.dumps(row) + "\n")

        page = page + 1

    file1.close()


def epoch_last_run():
    try:
        f = open('/tmp/tds22doctor_epoch_last_run.txt', 'r')
        return int(f.read())
    except ValueError as e:
        return 0
    except FileNotFoundError as e:
        return 0


def validators_api() -> str:
    _output, _error, _exit_code = run_command_with_retry(
        "{} validators --output json-compact -u {} --rpc-timeout={}".format(
        SOLANA_BINARY,
        RPC_URL,
        RPC_TIMEOUT
    ))

    return json.loads(_output)


def doctor_stakes() -> str:
    _output, _error, _exit_code = run_command_with_retry("{} stakes --withdraw-authority {} --output json-compact -u {} --rpc-timeout={}".format(
            SOLANA_BINARY,
            WITHDRAW_AUTHORITY,
            RPC_URL,
            RPC_TIMEOUT
        ))

    return json.loads(_output)


# read withdraw authority address from cli
WITHDRAW_AUTHORITY = shell_command("{} address -k {}".format(SOLANA_BINARY, AUTH_FILE))

# Read epoch data
output, error, exit_code = run_command_with_retry("{} epoch-info --output json-compact -u {}".format(
    SOLANA_BINARY,
    RPC_URL
))
epoch_info = json.loads(output)

epoch = int(epoch_info['epoch'])
epoch_completed_perc = int(epoch_info['slotIndex']) / int(epoch_info['slotsInEpoch']) * 100

print('Last epoch run: {}'.format(epoch_last_run()))
print('Current epoch: {}'.format(epoch))

# if epoch_last_run() == epoch:
#     print('Epoch already executed, skipping.')
#     exit(0)

# Refresh the validators cache from solana.org
vercel_api_download_all()
print('Build SFDP skip validators list/blacklist...')
with open(file_vercel, 'r') as file:
    for line in file:
        row = json.loads(line)

        # Allow Pending and TestnetOnboarded (skip Approved, Rejected etc)
        if row['state'] == 'Pending' or row['state'] == 'TestnetOnboarded':
            continue

        # Sometimes entries show up two times (especially in the end somewhere from offset 9200+)
        # Generally API is buggy, does not show ALL participants.
        # We query here, so we easy can skip already onboarded validators etc.
        if row['testnetPubkey'] not in sfdp_identity_skip_list:
            sfdp_identity_skip_list.append(row['testnetPubkey'])


if epoch_completed_perc < EPOCH_MINIMUM_PERC_PASSED:
    print('Too early on epoch: {}%'.format(epoch_completed_perc))
    exit(0)

validators_json = validators_api()
doctor_stakes_json = doctor_stakes()

stakes = {
    'inactive': [],
    'active': [],
    'activating': [],
    'deactivating': [],
}

for stake in doctor_stakes_json:
    stakeType = stake['stakeType']

    stake['balance'] = stake['accountBalance'] / 1000000000
    # stake['stake_active'] = stake['activeStake'] / 1000000000

    if stakeType == 'Initialized':
        stakes['inactive'].append(stake)
        continue

    if "activationEpoch" in stake and stake['activationEpoch'] == epoch and "deactivationEpoch" in stake and stake['deactivationEpoch'] == epoch:
        print('Stuck? activated and deactivated same epoch: {}'.format(stake['stakePubkey']))
        stakes['inactive'].append(stake)
        continue

    if "activationEpoch" in stake and stake['activationEpoch'] == epoch:
        stakes['activating'].append(stake)
        continue

    if "deactivationEpoch" in stake and stake['deactivationEpoch'] == epoch:
        stakes['deactivating'].append(stake)
        continue

    if "deactivationEpoch" in stake and not "activeStake" in stake and stake['deactivationEpoch'] < epoch:
        stakes['inactive'].append(stake)
        continue

    if "activeStake" in stake:
        stakes['active'].append(stake)
        continue


validators_eligible = []
for validator in validators_json['validators']:
    identity = validator['identityPubkey']
    vote = validator['voteAccountPubkey']
    stake_active = validator['activatedStake'] / 1000000000

    validator['stake_active'] = stake_active

    if identity in sfdp_identity_skip_list:
        debug and print('Validator "state" invalid on solana.org {} {}'.format(identity, vote))

        for stake in stakes['active']+stakes['activating']:
            if stake['delegatedVoteAccountAddress'] == vote:
                print('Deactivate "active" stake from SFDP invalid validator. {} {}'.format(identity, vote))
                command_string = solana_deactivate_string(stake['stakePubkey'])
                print(" - {}".format(command_string))
                if do_process:
                    print('Deactivate "active" stake from SFDP invalid validator. {} {}'.format(identity, vote))
                    output, error, exit_code = run_command_with_retry(command_string)
                    print(output)

        # for stake in stakes['activating']:
        #     if stake['delegatedVoteAccountAddress'] == vote:
        #         print('Deactivate "activating" from SFDP invalid validator. {} {}'.format(identity, vote))
        #         command_string = solana_deactivate_string(stake['stakePubkey'])
        #         print(" - {}".format(command_string))
        #         if do_process:
        #             print('Deactivate "activating" from SFDP invalid validator. {} {}'.format(identity, vote))
        #             # output, error, exit_code = run_command_with_retry(command_string)
        #             # print(output)
        continue

    # Maybe some longer delinquent nodes, skip
    if validator['epochCredits'] < 500:
        debug and print('Too low vote credits: {} {}'.format(identity, vote))
        continue

    stake_activating_matched = False
    for stake in stakes['activating']:
        if stake['delegatedVoteAccountAddress'] == vote:
            print('Validator already in activating mode. {} {}'.format(identity, vote))
            stake_activating_matched = True
            continue

    if stake_activating_matched:
        continue

    stake_active_needs_deactivate = False
    for stake in stakes['active']:
        if stake['delegatedVoteAccountAddress'] == vote:
            if stake_active - stake['balance'] > MAX_STAKE_ACTIVE:
                print('Can deactivate, enought active stake: {} SOL'.format(stake_active))
                stake_active_needs_deactivate = stake
            continue

    if stake_active_needs_deactivate:
        command_string = solana_deactivate_string(stake_active_needs_deactivate['stakePubkey'])
        print(" - {}".format(command_string))
        if do_process:
            print("Doing deactivate-stake...")
            output, error, exit_code = run_command_with_retry(command_string)
            print(output)

        continue

    if stake_active > MAX_STAKE_ACTIVE:
        debug and print('Too high stake already ({} SOL): {} {}'.format(stake_active, identity, vote))
        continue

    lastCompletedSignupStep = False
    testnet_validator_api = {
        'lastCompletedSignupStep': None
    }

    while not lastCompletedSignupStep:
        testnet_validator_api = make_http_request_cached(
            "https://api.solana.org/api/validators/{}".format(identity),
            'testnet_identity_{}'.format(identity)
        )

        if 'message' in testnet_validator_api:
            # {'message': 'Validator with public key "XYZ" not found'}
            if ' not found' in testnet_validator_api['message']:
                debug and print(' - SKIP {}'.format(testnet_validator_api))
                lastCompletedSignupStep = True
                testnet_validator_api = {
                    'lastCompletedSignupStep': None
                }
                continue

            if 'Rate limited.' in testnet_validator_api['message']:
                print(testnet_validator_api)
                cache_remove('testnet_identity_{}'.format(identity))
                time.sleep(30)
                continue

        if testnet_validator_api['state'] != 'Pending' and testnet_validator_api['state'] != 'TestnetOnboarded':
            debug and print(' - SKIP invalid state "{}": {} {}'.format(testnet_validator_api['state'], identity, vote))
            lastCompletedSignupStep = True
            testnet_validator_api = {
                'lastCompletedSignupStep': None
            }
            continue

        lastCompletedSignupStep = True

    if testnet_validator_api['lastCompletedSignupStep'] is None:
        debug and print('Validator not found in SFDP/Invalid state {} {}'.format(identity, vote))
        continue

    # Basically user is already in "Earn bonus stake" stage, so skip, validator does not need our help
    if testnet_validator_api['lastCompletedSignupStep'] == 'a7_earn_testnet_bonus':
        debug and print('Validator already at a7 step, skip. {} {}'.format(identity, vote))

        for stake in stakes['active']+stakes['activating']:
            if stake['delegatedVoteAccountAddress'] == vote:
                print('Deactivate "active/activating" stake from SFDP a7step validator. {} {}'.format(identity, vote))
                command_string = solana_deactivate_string(stake['stakePubkey'])
                print(" - {}".format(command_string))
                if do_process:
                    print('Deactivate "active/activating" stake from SFDP a7step validator. {} {}'.format(identity, vote))
                    output, error, exit_code = run_command_with_retry(command_string)
                    print(output)
        continue

    validators_eligible.append(validator)

# print(validators_eligible)
# print(len(validators_eligible))
# exit(0)

print('Inactive: {}'.format(len(stakes['inactive'])))
print('Active: {}'.format(len(stakes['active'])))
print('Activating: {}'.format(len(stakes['activating'])))
print('Deactivating: {}'.format(len(stakes['deactivating'])))
print('Eligible validators: {}'.format(len(validators_eligible)))

limit_shuffled = len(validators_eligible)
if len(stakes['inactive']) < limit_shuffled:
    limit_shuffled = len(stakes['inactive'])

validators_eligible_shuffled = random.sample(validators_eligible, limit_shuffled)

index = 0
for validator in validators_eligible_shuffled:
    identity = validator['identityPubkey']
    vote = validator['voteAccountPubkey']

    try:
        stake = stakes['inactive'][index]
    except IndexError:
        print('No more "inactive" stake accounts left')
        break

    command_string = solana_delegate_string(stake['stakePubkey'], vote)
    print(' - {}'.format(command_string))
    if do_process:
        print("Doing delegate-stake...")
        output, error, exit_code = run_command_with_retry(command_string)
        print(output)

    index = index + 1

write_to_file('/tmp/tds22doctor_epoch_last_run.txt', str(epoch))

