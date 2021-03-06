Configuration
=============
Both server and agent use the ``scrapydd.conf`` file for system configuration.
The file will be looked up in the following locations:

* /etc/scrapydd/scrapydd.conf
* /etc/scrapyd/conf.d/*
* scrapydd.conf
* ~/.scrapydd.conf


Server
------
Server configurations should appears under the ``[server]`` section.


bind_address
~~~~~~~~~~~~~~
The ipaddress which web server bind on. Default: ``0.0.0.0``

bind_port
~~~~~~~~~~
The port web server running on. Default: ``6800``

client_validation
~~~~~~~~~~~~~~~~~~
Whether validate client's certificate on SSL, Default: ``false``

debug
~~~~~~
Whether run server on debug mode. Debug mode will set logging level to DEBUG.
Default: ``false``.

https_port
~~~~~~~~~~~
HTTPS port to listen on, specify this key will enable SSL mode.

Default: None

server_name
~~~~~~~~~~~~
Server's hostname.
When SSL enabled, the public certificate will be loaded as filename `server_name`.crt and
private certificate will be loaded as filename `server_name`.key in the ``keys`` folder.
Default: ``localhost``



Agent
-----
Agent configurations should appears under the ``[agent]`` section.

debug
~~~~~~~~
Whether run agent on debug mode. Debug mode will set logging level to DEBUG. Default: ``false``

server
~~~~~~~~~
The IP address or hostname of the server which this agent connect to. Default: ``localhost``

server_port
~~~~~~~~~~~~~~
The port of server. Default: ``6800``

slots
~~~~~~~~
How many concurrent jobs the agent would run. Default: ``1``

request_timeout
~~~~~~~~~~~~~~~~
Request timeout in seconds when communicating to server. Default: ``60``


Example
--------

Here is an example configuration file with all the defaults::

    [server]
    bind_address = 0.0.0.0
    bind_port = 6800
    debug = false

    [agent]
    server = localhost
    server_port = 6800
    debug = false
    slots = 1
