# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# Name:        sfp_censys
# Purpose:     Query Censys.io API
#
# Author:      Steve Micallef
#
# Created:     01/02/2017
# Copyright:   (c) Steve Micallef 2017
# Licence:     GPL
# -------------------------------------------------------------------------------

import base64
import json
import time
from datetime import datetime

from netaddr import IPNetwork

from spiderfoot import SpiderFootEvent, SpiderFootPlugin


class sfp_censys(SpiderFootPlugin):

    meta = {
        'name': "Censys",
        'summary': "Obtain information from Censys.io",
        'flags': ["apikey"],
        'useCases': ["Investigate", "Passive"],
        'categories': ["Search Engines"],
        'dataSource': {
            'website': "https://censys.io/",
            'model': "FREE_AUTH_LIMITED",
            'references': [
                "https://censys.io/api",
                "https://censys.io/product",
                "https://censys.io/ipv4"
            ],
            'apiKeyInstructions': [
                "Visit https://censys.io/",
                "Register a free account",
                "Navigate to https://censys.io/account",
                "Click on 'API'",
                "The API key combination is listed under 'API ID' and 'Secret'"
            ],
            'favIcon': "https://censys.io/assets/favicon.png",
            'logo': "https://censys.io/assets/logo.png",
            'description': "Discover exposures and other common entry points for attackers.\n"
            "Censys scans the entire internet constantly, including obscure ports. "
            "We use a combination of banner grabs and deep protocol handshakes "
            "to provide industry-leading visibility and an accurate depiction of what is live on the internet.",
        }
    }

    # Default options
    opts = {
        "censys_api_key_uid": "",
        "censys_api_key_secret": "",
        'delay': 3,
        "age_limit_days": 90
    }

    # Option descriptions
    optdescs = {
        "censys_api_key_uid": "Censys.io API UID.",
        "censys_api_key_secret": "Censys.io API Secret.",
        'delay': 'Delay between requests, in seconds.',
        "age_limit_days": "Ignore any records older than this many days. 0 = unlimited."
    }

    # Be sure to completely clear any class variables in setup()
    # or you run the risk of data persisting between scan runs.
    results = None
    errorState = False

    def setup(self, sfc, userOpts=dict()):
        self.sf = sfc
        self.results = self.tempStorage()

        # Clear / reset any other class member variables here
        # or you risk them persisting between threads.
        for opt in list(userOpts.keys()):
            self.opts[opt] = userOpts[opt]

    # What events is this module interested in for input
    def watchedEvents(self):
        return ["IP_ADDRESS", "INTERNET_NAME", "NETBLOCK_OWNER"]

    # What events this module produces
    def producedEvents(self):
        return ["BGP_AS_MEMBER", "TCP_PORT_OPEN", "OPERATING_SYSTEM",
                "WEBSERVER_HTTPHEADERS", "NETBLOCK_MEMBER", "GEOINFO",
                'RAW_RIR_DATA']

    # https://censys.io/api
    # https://censys.io/api/v1/docs/view
    def query(self, qry, querytype):
        if querytype == "ip":
            querytype = "ipv4/{0}"
        if querytype == "host":
            querytype = "websites/{0}"

        secret = self.opts['censys_api_key_uid'] + ':' + self.opts['censys_api_key_secret']
        b64_val = base64.b64encode(secret.encode('utf-8'))

        headers = {
            'Authorization': 'Basic %s' % b64_val.decode('utf-8')
        }

        res = self.sf.fetchUrl('https://censys.io/api/v1/view/' + querytype.format(qry),
                               timeout=self.opts['_fetchtimeout'],
                               useragent="SpiderFoot",
                               headers=headers)

        # API rate limit: 0.4 actions/second (120.0 per 5 minute interval)
        time.sleep(self.opts['delay'])

        if res['code'] in ["400", "429", "500", "403"]:
            self.sf.error("Censys.io API key seems to have been rejected or you have exceeded usage limits for the month.", False)
            self.errorState = True
            return None

        if res['content'] is None:
            self.sf.info("No Censys.io info found for " + qry)
            return None

        try:
            info = json.loads(res['content'])
        except Exception as e:
            self.sf.error(f"Error processing JSON response from Censys.io: {e}", False)
            return None

        # print(str(info))
        return info

    # Handle events sent to this module
    def handleEvent(self, event):
        eventName = event.eventType
        srcModuleName = event.module
        eventData = event.data

        if self.errorState:
            return None

        self.sf.debug(f"Received event, {eventName}, from {srcModuleName}")

        if self.opts['censys_api_key_uid'] == "" or self.opts['censys_api_key_secret'] == "":
            self.sf.error("You enabled sfp_censys but did not set an API uid/secret!", False)
            self.errorState = True
            return None

        # Don't look up stuff twice
        if eventData in self.results:
            self.sf.debug(f"Skipping {eventData}, already checked.")
            return None

        self.results[eventData] = True

        qrylist = list()
        if eventName.startswith("NETBLOCK_"):
            for ipaddr in IPNetwork(eventData):
                qrylist.append(str(ipaddr))
                self.results[str(ipaddr)] = True
        else:
            qrylist.append(eventData)

        for addr in qrylist:
            if self.checkForStop():
                return None

            if eventName in ["IP_ADDRESS", "NETBLOCK_OWNER"]:
                qtype = "ip"
            else:
                qtype = "host"

            rec = self.query(addr, qtype)

            if rec is None:
                continue

            if 'error' in rec:
                if rec['error_type'] == "unknown":
                    self.sf.debug("Censys returned no data for " + addr)
                else:
                    self.sf.error("Censys returned an unexpected error: " + rec['error_type'], False)
                continue

            self.sf.debug("Found results in Censys.io")

            # For netblocks, we need to create the IP address event so that
            # the threat intel event is more meaningful.
            if eventName.startswith('NETBLOCK_'):
                pevent = SpiderFootEvent("IP_ADDRESS", addr, self.__name__, event)
                self.notifyListeners(pevent)
            else:
                pevent = event

            e = SpiderFootEvent("RAW_RIR_DATA", str(rec), self.__name__, pevent)
            self.notifyListeners(e)

            try:
                # Date format: 2016-12-24T07:25:35+00:00'
                created_dt = datetime.strptime(rec.get('updated_at', "1970-01-01T00:00:00+00:00"), '%Y-%m-%dT%H:%M:%S+00:00')
                created_ts = int(time.mktime(created_dt.timetuple()))
                age_limit_ts = int(time.time()) - (86400 * self.opts['age_limit_days'])

                if self.opts['age_limit_days'] > 0 and created_ts < age_limit_ts:
                    self.sf.debug("Record found but too old, skipping.")
                    continue

                if 'location' in rec:
                    location = ', '.join([_f for _f in [rec['location'].get('city'), rec['location'].get('province'), rec['location'].get('postal_code'), rec['location'].get('country')] if _f])
                    if location:
                        e = SpiderFootEvent("GEOINFO", location, self.__name__, pevent)
                        self.notifyListeners(e)

                if 'headers' in rec:
                    dat = json.dumps(rec['headers'], ensure_ascii=False)
                    e = SpiderFootEvent("WEBSERVER_HTTPHEADERS", dat, self.__name__, pevent)
                    e.actualSource = addr
                    self.notifyListeners(e)

                if 'autonomous_system' in rec:
                    dat = str(rec['autonomous_system']['asn'])
                    e = SpiderFootEvent("BGP_AS_MEMBER", dat, self.__name__, pevent)
                    self.notifyListeners(e)

                    dat = rec['autonomous_system']['routed_prefix']
                    e = SpiderFootEvent("NETBLOCK_MEMBER", dat, self.__name__, pevent)
                    self.notifyListeners(e)

                if 'protocols' in rec:
                    for p in rec['protocols']:
                        if 'ip' not in rec:
                            continue
                        dat = rec['ip'] + ":" + p.split("/")[0]
                        e = SpiderFootEvent("TCP_PORT_OPEN", dat, self.__name__, pevent)
                        self.notifyListeners(e)

                if 'metadata' in rec:
                    if 'os_description' in rec['metadata']:
                        dat = rec['metadata']['os_description']
                        e = SpiderFootEvent("OPERATING_SYSTEM", dat, self.__name__, pevent)
                        self.notifyListeners(e)
            except BaseException as e:
                self.sf.error("Error encountered processing record for " + eventData + " (" + str(e) + ")", False)

# End of sfp_censys class
