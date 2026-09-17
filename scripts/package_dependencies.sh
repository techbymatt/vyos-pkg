#!/usr/bin/env bash

case "$1" in
  hvinfo) echo "gnat gprbuild" ;;
  ipaddrcheck) echo "check libcidr-dev" ;;
  libnss-tacplus) echo "libaudit-dev libpam-tacplus-dev libtac-dev libtacplus-map-dev" ;;
  libpam-tacplus) echo "autoconf-archive libaudit-dev libpam-dev libssl-dev libtacplus-map-dev" ;;
  libtacplus-map) echo "autoconf-archive libaudit-dev" ;;
  vyatta-bash) echo "bison libncurses5-dev" ;;
  vyatta-biosdevname) echo "libpci-dev" ;;
  vyatta-cfg) echo "bison flex libboost-filesystem-dev libglib2.0-dev" ;;
  vyos-http-api-tools) echo "dh-virtualenv" ;;
  *) echo "" ;;
esac
