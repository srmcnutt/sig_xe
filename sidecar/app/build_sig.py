#!/usr/bin/env python

"""
Authors: Nick Russo (nickrus@cisco.com) and Steve McNutt (stmcnutt@cisco.com)
Purpose: Provision new Umbrella SIG tunnels or rekey existing ones then
configure the Cisco device side using IOS or ASA platforms. Also performs
a validation to ensure the tunnel was formed successfully.

Copyright (c) 2021 Cisco Systems, Inc. and its affiliates
All rights reserved
"""

from datetime import datetime
import json
import time
import random
import string
import requests
import jsonschema
from netmiko import Netmiko
from jinja2 import Environment, FileSystemLoader


def main(**kwargs):
    """
    Execution starts here. The kwargs represent the tunnel parameters
    required for everything to be provisioned correctly.
    """

    with open("tunnel_params_schema.json", "r") as handle:
        schema = json.load(handle)

    try:
        jsonschema.validate(instance=kwargs, schema=schema)
        print("Schema validation passed")
    except jsonschema.exceptions.ValidationError as exc:
        print(f"Schema validation failed: {exc}")
        return {"error": str(exc)}

    # Map umbrella device types to netmiko device types
    DEVICE_MAP = {"ISR": "cisco_ios", "ASA": "cisco_asa"}

    # Build the base URL for interacting with Umbrella
    umbrella_base_url = (
        f"https://management.api.umbrella.com/v1/"
        f"organizations/{kwargs['umbrella_org_id']}/tunnels"
    )

    # Build the HTTP basic auth tuple and headers
    auth = (kwargs["umbrella_api_key"], kwargs["umbrella_api_secret"])
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Get existing SIG tunnels from Umbrella
    get_sig = requests.get(umbrella_base_url, auth=auth, headers=headers)
    get_sig.raise_for_status()

    # Generate an IKEv2 PSK; this is required whether we are adding
    # a new tunnel or rekeying an existing tunnel
    secret = _generate_password()
    dev = kwargs["device_name"]

    # Loop over all SIG tunnels returned
    for cur_sig in get_sig.json():

        # SIG tunnel already exists; perform rekey
        if cur_sig["name"] == dev:
            print(f"SIG to {dev} already present; rekeying tunnel")

            # Assemble rekey body with custom secret
            body = {
                "deprecateCurrentKeys": True,
                "autoRotate": False,
                "psk": {"idPrefix": dev, "secret": secret},
            }

            # Define API resource string for rekeying the tunnel by ID
            url = f"{umbrella_base_url}/{cur_sig['id']}/keys"
            config_type = "rekey"
            break

    # For loop exhausted without hitting a break; must add new SIG tunnel
    else:
        print(f"SIG to {dev} not present; adding new")
        body = {
            "name": dev,
            "deviceType": kwargs["device_type"],
            "transport": {"protocol": "IPSec"},
            "authentication": {
                "type": "PSK",
                "parameters": {"idPrefix": dev, "secret": secret},
            },
        }

        # Define API resource string for adding a new tunnel
        url = umbrella_base_url
        config_type = "full"

    # Issue POST request to build or rekey a tunnel, then store response body
    mod_sig = requests.post(url, auth=auth, headers=headers, json=body)
    mod_sig.raise_for_status()
    sig = mod_sig.json()
    # print(json.dumps(sig, indent=2))

    # Load umbrella IPs from file
    with open("umbrella_sites.json", "r") as handle:
        umbrella_sites = json.load(handle)

    # Setup the jinja2 templating environment and render the template
    j2_env = Environment(
        loader=FileSystemLoader("."), trim_blocks=True, autoescape=True
    )
    template = j2_env.get_template(
        f"templates/{kwargs['device_type'].lower()}_{config_type}.j2"
    )

    # Assemble the data necessary for the templating process
    data = {
        "sig": sig,
        "now": str(datetime.utcnow()),
        "umbrella_sites": umbrella_sites,
        "tunnel_src_intf": kwargs["tunnel_src_intf"],
        "tunnel_dest_ip": kwargs["tunnel_dest_ip"],
        "tunnel_ulay_nhop": kwargs["tunnel_ulay_nhop"],
        "tunnel_vrf": kwargs["tunnel_vrf"],
    }
    new_config = template.render(data=data)

    # Create netmiko SSH connection handler to access the device
    conn = Netmiko(
        host=kwargs["device_mgmt_ip"],
        username=kwargs["device_username"],
        password=kwargs["device_password"],
        device_type=DEVICE_MAP[kwargs["device_type"]],
    )

    print(f"Logged into {conn.find_prompt()} successfully")
    conn.send_config_set(new_config.split("\n"))
    tun_dest = kwargs["tunnel_dest_ip"]

    # TODO helps to ensure rekey worked, but is disruptive
    # conn.send_command(f"clear crypto ikev2 sa remote {tun_dest}")

    # Verify tunnel on IOS side, could take a few minutes
    for i in range(12):
        time.sleep(10)
        print(f"Attempt {i+1} to verify client-side IPsec connectivity")
        session_resp = conn.send_command(
            f"show crypto session remote {tun_dest} | include ^Session_status"
        )

        # If the crypto session is up, continue
        if "UP-ACTIVE" in session_resp:
            print(f"OK: Client-side SIG to {tun_dest} is up")

            # If the FIB entry to the Internet looks correct, continue
            if "attached to Tunnel100" in conn.send_command(
                "show ip cef 8.8.8.8"
            ):
                print("OK: Upstream default routing to Umbrella is correct")

                # If the ping test through the SIG tunnel succeeds, continue
                if "100 percent" in conn.send_command(
                    "ping 8.8.8.8 size 1400 df-bit source loopback999"
                ):
                    print("OK: Ping to 8.8.8.8 succeeded")
                    break
                    
                print("FAIL: Ping to 8.8.8.8 failed")

            # Else, the FIB entry was not correct
            else:
                print("FAIL: Upstream default routing to Umbrella is incorrect")

    # Loop exhausted; crypto session never came up
    else:
        print(f"FAIL: Client-side SIG to {tun_dest} never came up")

    # Close connection to router; we are done with it
    conn.disconnect()

    # Verify the tunnel is healthy from Umbrella's perspective
    for i in range(6):
        time.sleep(5)
        print(f"Attempt {i+1} to verify Umbrella-side IPsec connectivity")

        # Get the tunnel data for the newly added/rekeyed tunnel
        get_sig = requests.get(
            f"{umbrella_base_url}/{sig['id']}", auth=auth, headers=headers
        )
        get_sig.raise_for_status()
        meta = get_sig.json()["meta"]

        if "state" in meta and meta["state"]["status"] == "UP":
            print(f"OK: Umbrella-side SIG to {dev} is up")
            break
    else:
        print(f"FAIL: Umbrella-side SIG to {dev} never came up")

    print("SIG status report:")
    print(json.dumps(meta["state"], indent=2))
    return meta["state"]


def _generate_password(length=16):
    """
    Generate password of specified length with at least one digit,
    uppercase letter, and lowercase letter. This is used as the
    IKEv2 PSK on both sides of the tunnel.
    """

    # Need 1 each: uppercase, lowercase, digit
    pw_minimum = [
        random.choice(string.digits),
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
    ]

    # Fill in the remaining N-3 characters
    pw_rest = [
        random.choice(string.digits + string.ascii_letters)
        for i in range(length - 3)
    ]

    # Randomize the letters and return the password as a string
    pw_list = pw_minimum + pw_rest
    random.shuffle(pw_list)
    return "".join(pw_list)


if __name__ == "__main__":

    # Load test data from home directory
    with open("/home/centos/tunnel_params.json", "r") as handle:
        tunnel_params = json.load(handle)

    main(**tunnel_params)
