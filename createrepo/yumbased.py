#!/usr/bin/python -tt

import os
import sys
import struct
import rpm
import types
import re
import xml.sax.saxutils

from yum.packages import YumLocalPackage
from yum.Errors import *
from yum import misc
from rpmUtils.transaction import initReadOnlyTransaction
from rpmUtils.miscutils import flagToString, stringToVersion
import libxml2
import utils

#FIXME - merge into class with config stuff
fileglobs = ['.*bin\/.*', '^\/etc\/.*', '^\/usr\/lib\/sendmail$']
file_re = []
for glob in fileglobs:
    file_re.append(re.compile(glob))        

dirglobs = ['.*bin\/.*', '^\/etc\/.*']
dir_re = []
for glob in dirglobs:
    dir_re.append(re.compile(glob))        


class CreateRepoPackage(YumLocalPackage):
    def __init__(self, ts, package):
        YumLocalPackage.__init__(self, ts, package)
        self._checksum = None        
        self._stat = os.stat(package)
        self.filetime = str(self._stat[-2])
        self.packagesize = str(self._stat[6])
        self._hdrstart = None
        self._hdrend = None
        self.xml_node = libxml2.newDoc("1.0")
                
        
    def _xml(self, item):
        item = utils.utf8String(item)
        return xml.sax.saxutils.escape(item)
        
    def _do_checksum(self):
        if not self._checksum:
            self._checksum = misc.checksum('sha', self.localpath)
            
        return self._checksum
    checksum = property(fget=lambda self: self._do_checksum())
    
    def _get_header_byte_range(self):
        """takes an rpm file or fileobject and returns byteranges for location of the header"""
        if self._hdrstart and self._hdrend:
            return (self._hdrstart, self._hdrend)
      
           
        fo = open(self.localpath, 'r')
        #read in past lead and first 8 bytes of sig header
        fo.seek(104)
        # 104 bytes in
        binindex = fo.read(4)
        # 108 bytes in
        (sigindex, ) = struct.unpack('>I', binindex)
        bindata = fo.read(4)
        # 112 bytes in
        (sigdata, ) = struct.unpack('>I', bindata)
        # each index is 4 32bit segments - so each is 16 bytes
        sigindexsize = sigindex * 16
        sigsize = sigdata + sigindexsize
        # we have to round off to the next 8 byte boundary
        disttoboundary = (sigsize % 8)
        if disttoboundary != 0:
            disttoboundary = 8 - disttoboundary
        # 112 bytes - 96 == lead, 8 = magic and reserved, 8 == sig header data
        hdrstart = 112 + sigsize  + disttoboundary
        
        fo.seek(hdrstart) # go to the start of the header
        fo.seek(8,1) # read past the magic number and reserved bytes

        binindex = fo.read(4) 
        (hdrindex, ) = struct.unpack('>I', binindex)
        bindata = fo.read(4)
        (hdrdata, ) = struct.unpack('>I', bindata)
        
        # each index is 4 32bit segments - so each is 16 bytes
        hdrindexsize = hdrindex * 16 
        # add 16 to the hdrsize to account for the 16 bytes of misc data b/t the
        # end of the sig and the header.
        hdrsize = hdrdata + hdrindexsize + 16
        
        # header end is hdrstart + hdrsize 
        hdrend = hdrstart + hdrsize 
        fo.close()
        self._hdrstart = hdrstart
        self._hdrend = hdrend
       
        return (hdrstart, hdrend)
        
    hdrend = property(fget=lambda self: self._get_header_byte_range()[1])
    hdrstart = property(fget=lambda self: self._get_header_byte_range()[0])
    
    def _dump_base_items(self, basedir, baseurl=None):
        """Takes an optional baseurl and required basedir.
           basedir is the relative path to remove from the location
           baseurl is whether or not this package already has a
           baseurl rather than just '.'"""
        
        # if we start seeing fullpaths in the location tag - this is the culprit
        if self.localpath.startswith(basedir):
            relpath = self.localpath.replace(basedir, '')
            if relpath[0] == '/': relpath = relpath[1:]
        else:
            relpath = self.localpath

        packager = url = ''
        if self.packager:
            packager = self._xml(self.packager)
        
        if self.url:
            url = self._xml(self.url)
                    
        msg = """
  <name>%s</name>
  <arch>%s</arch>
  <version epoch="%s" ver="%s" rel="%s"/>
  <checksum type="sha" pkgid="YES">%s</checksum>
  <summary>%s</summary>
  <description>%s</description>
  <packager>%s</packager>
  <url>%s</url>
  <time file="%s" build="%s"/>
  <size package="%s" installed="%s" archive="%s"/>""" % (self.name, 
         self.arch, self.epoch, self.ver, self.rel, self.checksum, 
         self._xml(self.summary), self._xml(self.description), packager, 
         url, self.filetime, self.buildtime, self.packagesize, self.size, 
         self.archivesize)
         

        if baseurl:
            msg += """<location xml:base="%s" href="%s"/>""" % (self._xml(baseurl), relpath)
        else:
            msg += """<location href="%s"/>""" % relpath
            
        return msg

    def _dump_format_items(self):
        msg = "  <format>\n"
        if self.license:
            msg += """    <rpm:license>%s</rpm:license>\n""" % self._xml(self.license)
        if self.vendor:
            msg += """    <rpm:vendor>%s</rpm:vendor>\n""" % self._xml(self.vendor)
        if self.group:
            msg += """    <rpm:group>%s</rpm:group>\n""" % self._xml(self.group)
        if self.buildhost:
            msg += """    <rpm:buildhost>%s</rpm:buildhost>\n""" % self._xml(self.buildhost)
        if self.sourcerpm:
            msg += """    <rpm:sourcerpm>%s</rpm:sourcerpm>\n""" % self._xml(self.sourcerpm)
        msg +="""    <rpm:header-range start="%s" end="%s"/>""" % (self.hdrstart,
                                                               self.hdrend)
        msg += self._dump_pco('provides')
        msg += self._dump_requires()
        msg += self._dump_pco('conflicts')         
        msg += self._dump_pco('obsoletes')         
        msg += self._dump_files(True)
        msg += """\n  </format>"""
        return msg

    def _dump_pco(self, pcotype):
           
        msg = ""
        mylist = getattr(self, pcotype)
        if mylist: msg = "\n    <rpm:%s>\n" % pcotype
        for (name, flags, (e,v,r)) in mylist:
            pcostring = '''      <rpm:entry name="%s"''' % name
            if flags:
                pcostring += ''' flags="%s"''' % flags
                if e:
                    pcostring += ''' epoch="%s"''' % e
                if v:
                    pcostring += ''' ver="%s"''' % v
                if r:
                    pcostring += ''' rel="%s"''' % r
                    
            pcostring += "/>\n"
            msg += pcostring
            
        if mylist: msg += "    </rpm:%s>" % pcotype
        return msg
    
    def _return_primary_files(self, list_of_files=None):

        returns = {}
        if list_of_files is None:
            list_of_files = self.returnFileEntries('file')
        for item in list_of_files:
            if item is None:
                continue
            for glob in file_re:
                if glob.match(item):
                    returns[item] = 1
        return returns.keys()

    def _return_primary_dirs(self):

        returns = {}
        for item in self.returnFileEntries('dir'):
            if item is None:
                continue
            for glob in dir_re:
                if glob.match(item):
                    returns[item] = 1
        return returns.keys()
        
        
    def _dump_files(self, primary=False):
        msg =""
        if not primary:
            files = self.returnFileEntries('file')
            dirs = self.returnFileEntries('dir')
            ghosts = self.returnFileEntries('ghost')
        else:
            files = self._return_primary_files()
            ghosts = self._return_primary_files(list_of_files = self.returnFileEntries('ghost'))
            dirs = self._return_primary_dirs()
                
        for fn in files:
            msg += """    <file>%s</file>\n""" % utils.utf8String(fn)
        for fn in dirs:
            msg += """    <file type="dir">%s</file>\n""" % utils.utf8String(fn)
        for fn in ghosts:
            msg += """    <file type="ghost">%s</file>\n""" % utils.utf8String(fn)
        
        return msg

    def _is_pre_req(self, flag):
        """check the flags for a requirement, return 1 or 0 whether or not requires
           is a pre-requires or a not"""
        # FIXME this should probably be put in rpmUtils.miscutils since 
        # - that's what it is
        newflag = flag
        if flag is not None:
            newflag = flag & 64
            if newflag == 64:
                return 1
            else:
                return 0
        return 0
                
    def _dump_requires(self):
        """returns deps in format"""
        name = self.hdr[rpm.RPMTAG_REQUIRENAME]
        lst = self.hdr[rpm.RPMTAG_REQUIREFLAGS]
        flag = map(flagToString, lst)
        pre = map(self._is_pre_req, lst)
        lst = self.hdr[rpm.RPMTAG_REQUIREVERSION]
        vers = map(stringToVersion, lst)
        if name is not None:
            lst = zip(name, flag, vers, pre)
        mylist = misc.unique(lst)

        msg = ""

        if mylist: msg = "\n    <rpm:requires>\n"
        for (name, flags, (e,v,r),pre) in mylist:
            prcostring = '''      <rpm:entry name="%s"''' % name
            if flags:
                prcostring += ''' flags="%s"''' % flags
                if e:
                    prcostring += ''' epoch="%s"''' % e
                if v:
                    prcostring += ''' ver="%s"''' % v
                if r:
                    prcostring += ''' rel="%s"''' % r
            if pre:
                prcostring += ''' pre="%s"''' % pre
                    
            prcostring += "/>\n"
            msg += prcostring
            
        if mylist: msg += "    </rpm:requires>"
        return msg

    def _dump_changelog(self):
        if not self.changelog:
            return ""
        msg = "\n"
        for (ts, author, content) in self.changelog:            
            c = self.xml_node.newChild(None, "changelog", None)
            c.addContent(utils.utf8String(content))
            c.newProp('author', utils.utf8String(author))
            c.newProp('date', str(ts))
            msg += c.serialize()
            c.unlinkNode()
            c.freeNode()  
            del c
        return msg                                                 

    def do_primary_xml_dump(self, basedir, baseurl=None):
        msg = """\n<package type="rpm">"""
        msg += self._dump_base_items(basedir, baseurl)
        msg += self._dump_format_items()
        msg += """\n</package>"""
        return msg

    def do_filelists_xml_dump(self):
        msg = """\n<package pkgid="%s" name="%s" arch="%s">
    <version epoch="%s" ver="%s" rel="%s"/>\n""" % (self.checksum, self.name, 
                                     self.arch, self.epoch, self.ver, self.rel)
        msg += self._dump_files()
        msg += "</package>\n"
        return msg

    def do_other_xml_dump(self):   
        msg = """\n<package pkgid="%s" name="%s" arch="%s">
    <version epoch="%s" ver="%s" rel="%s"/>\n""" % (self.checksum, self.name, 
                                     self.arch, self.epoch, self.ver, self.rel)
        msg += self._dump_changelog()
        msg += "\n</package>\n"
        return msg
       
        
           
