#!/bin/bash
exec ssh -i /home/tejeswar/.ssh/nso_key \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o "ProxyCommand=ssh -i /home/tejeswar/.ssh/nso_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p ubuntu@194.47.155.186" \
    "$@"
