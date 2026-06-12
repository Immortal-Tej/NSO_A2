#!/bin/bash
NODE_IPS=($(cat /opt/node_ips.txt | tr ',' ' '))
IDX=0
while true; do
  TARGET=${NODE_IPS[$IDX]}
  IDX=$(( (IDX+1) % ${#NODE_IPS[@]} ))
  socat UDP-RECVFROM:6000,fork UDP-SENDTO:${TARGET}:161 &
  wait $!
done
