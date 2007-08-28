# Copyright (C) 2007 AG Projects.
#

import unittest
from common import XCAPTest

resource_list_xml = """<?xml version="1.0" encoding="UTF-8"?>
   <resource-lists xmlns="urn:ietf:params:xml:ns:resource-lists">
     <list name="friends">
      <entry uri="sip:joe@example.com">
        <display-name>Joe Smith</display-name>
      </entry>
      <entry uri="sip:nancy@example.com">
        <display-name>Nancy Gross</display-name>
      </entry>
      <entry uri="sip:petri@example.com">
        <display-name>Petri Aukia</display-name>
      </entry>
     </list>
   </resource-lists>"""

class NSBindingsTest(XCAPTest):

    def test_ns_bindings(self):
        self.put_resource('resource-lists', resource_list_xml)
        self.assertStatus([200, 201])

        self.get_resource('resource-lists', '/resource-lists/list[@name="friends"]/namespace::*')
        self.assertStatus(200)
        self.assertInBody(list_element_xml)
        #self.assertBody(list_element_xml)
        self.assertHeader('ETag')
        self.assertHeader('Content-type', 'application/xcap-ns+xml')

def suite():
    suite = unittest.TestLoader().loadTestsFromTestCase(NSBindingsTest)
    return suite

if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite())
