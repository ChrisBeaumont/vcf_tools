#!/usr/bin/env python

# BEGIN_COPYRIGHT
#
# This file is part of SciDB.
# Copyright (C) 2008-2014 SciDB, Inc.
#
# SciDB is free software: you can redistribute it and/or modify
# it under the terms of the AFFERO GNU General Public License as published by
# the Free Software Foundation.
#
# SciDB is distributed "AS-IS" AND WITHOUT ANY WARRANTY OF ANY KIND,
# INCLUDING ANY IMPLIED WARRANTY OF MERCHANTABILITY,
# NON-INFRINGEMENT, OR FITNESS FOR A PARTICULAR PURPOSE. See
# the AFFERO GNU General Public License for the complete license terms.
#
# You should have received a copy of the AFFERO GNU General Public License
# along with SciDB.  If not, see <http://www.gnu.org/licenses/agpl-3.0.html>
#
# END_COPYRIGHT

# Initial Developer: GP
# Created: September, 2012

import optparse
import csv
import cStringIO
import StringIO as pyStringIO
import fcntl
import subprocess
import signal
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import socket
import select

from scidblib import scidb_schema

####################
# Module Variables #
####################
opts = None
inputFile = sys.stdin
childProcesses = []
instances = []
outputBase = ""
dlfFragmentName = ""
sciDbBinFolder = ""
hostAddresses = ""
tmpDir = ""
runId = ""
devNull = None
loadAttrs = []
loadDims = []

def setupModuleVariables():

    global inputFile, childProcesses, instances, outputBase, dlfFragmentName, \
        sciDbBinFolder, hostAddresses, tmpDir, runId, devNull

    # Options should have been parsed by now.
    assert opts is not None

    # Reset to initial values.
    inputFile = sys.stdin
    childProcesses = []
    instances = []
    outputBase = ""
    dlfFragmentName = ""
    sciDbBinFolder = ""
    hostAddresses = ""
    if devNull is None:
        devNull = open('/dev/null', 'rb+')

    # Unique per-run tmpDir and runId let many loadcsv calls all use stdin.
    parent = opts.temp_dir if opts.temp_dir else '/tmp'
    prefix = 'loadcsv.'
    suffix = '.d'
    tmpDir = tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=parent)
    runId = tmpDir[len(os.sep.join((parent, prefix))):-len(suffix)]

    if opts.output_base:
        # User specified something for the output base file name.
        if os.path.basename(opts.output_base):
            # Output file was specified.
            outputBase = opts.output_base
        else:
            # Only an output folder was provided. No filename.
            if opts.input_file and isinstance(opts.input_file, basestring):
                outputBase += os.path.basename(opts.input_file)
            else:
                outputBase += "stdin.csv"
    else:
        # User did not specify an output base file name.
        if not opts.use_csv_files:
            outputBase = tmpDir + '/'
        if opts.input_file and isinstance(opts.input_file, basestring):
            # Just use the same name as the input file as our base.
            outputBase += os.path.basename(opts.input_file)
        else:
            # Use a generic name (input data is coming from stdin).
            outputBase += "stdin.csv"
    dlfFragmentName = '.'.join((os.path.basename(outputBase), runId, "dlf"))
    sciDbBinFolder = opts.db_root + "/bin/"
    hostAddresses = getHostAddresses()

###########
# flatten #
###########
def flatten(collection):
    result = []
    for item in collection:
        if isinstance(item, (tuple, list)):
            result.extend(flatten(item))
        else:
            result.append(item)
    return result

####################
# getHostAddresses #
####################
def getHostAddresses():
    addresses = ["localhost"]
    try:
        addresses.extend(flatten(socket.gethostbyaddr(socket.gethostname())))
    except Exception, e:
        addresses.append(socket.gethostname())
    return addresses

###############
# showVersion #
###############
def showVersion():
    if opts.show_version:
        cmd = sciDbBinFolder + "scidb --version"
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
        childProcesses.append(p)
        retVal = p.wait()
        if retVal != 0:
            err = "Failed to obtain SciDB version information."
            if p and p.stderr:
                err = "%s\n%s" % (err, p.stderr.read())
            raise Exception(err)
        print(p.stdout.read());

#################
# getLoadSchema #
#################
def getLoadSchema():
    global loadAttrs, loadDims
    if opts.load_schema:
        loadAttrs, loadDims = scidb_schema.parse(opts.load_schema)
    elif opts.load_name:
        # Query SciDB for the schema.
        logNormal("Retrieving load array schema from SciDB.")
        cmd = "\"%siquery\" -c %s -p %d -o text -aq \"show(%s)\"" % (
            sciDbBinFolder, opts.db_address, opts.db_port, opts.load_name)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             shell=True, close_fds=True, preexec_fn=os.setsid)
        childProcesses.append(p)
        retVal = p.wait()
        if retVal != 0:
            err = "Failed to obtain schema for load array."
            if p and p.stderr:
                err = "%s\n%s" % (err, p.stderr.read())
            raise Exception(err)
        arrayDef = p.stdout.read()
        m = re.search(r'<[^>]+>\s*\[[^\]]+\]', arrayDef)
        if not m:
            err = "Schema obtained from load array is corrupt: %s" % arrayDef.rstrip("\n")
            if p and p.stderr:
                err = "%s\n%s" % (err, p.stderr.read())
            raise Exception(err)
        logVerbose("Result: %s" % m.group(0))
        loadAttrs, loadDims = scidb_schema.parse(m.group(0))

################
# setChunkSize #
################
def setChunkSize():
    assert loadAttrs and loadDims
    assert len(loadDims) == 1
    cs = loadDims[0].chunk
    if cs != opts.chunk_size:
        logVerbose("Using chunk size of %d for load array based on load array schema definition." % cs)
        opts.chunk_size = cs

##################
# setTypePattern #
##################
def setTypePattern():
    """Compute a 'type pattern' based on the load schema."""
    # The only real use of the type pattern is to dictate quoting
    # behavior.  This behavior should be driven by the load schema,
    # not by command line arguments---so we override those.
    logNormal("Computing type-pattern from load schema.")
    assert loadAttrs and loadDims
    assert len(loadDims) == 1
    old_pattern = opts.type_pattern
    pattern = ''
    for attr in loadAttrs:
        if 'string' in attr.type:
            # Unclear if "nullable vs. not" matters, but don't lose the info.
            pattern += 's' if attr.nullable else 'S'
        elif 'char' in attr.type:
            pattern += 'c' if attr.nullable else 'C'
        else:
            pattern += 'N'
    if old_pattern and pattern != old_pattern:
        # The only reason for leaving -t (opts.type_pattern) around is
        # so customer scripts don't have to change.  Ignore it but warn.
        print "Warning: type pattern %s conflicts with load schema, using %s instead" % (
            old_pattern, pattern)
    opts.type_pattern = pattern

#################
# getSshCommand #
#################
# Generic function to construct SSH commands.
def getSshCommand(username, keyfile, host, port, bypass_key_check, command):
    sshCommand = "ssh -c arcfour256 "
    if bypass_key_check:
        sshCommand += "-o StrictHostKeyChecking=no "
    if port:
        sshCommand += "-p %d " % port
    if keyfile:
        sshCommand += "-i \"%s\" " % keyfile
    if username:
        sshCommand += "%s@" % username
    if host:
        sshCommand += "%s" % host
    if command:
        sshCommand += " %s" % command
    return sshCommand.replace("\\", "\\\\").replace("\"", "\\\"")

#############
# logNormal #
#############
def logNormal(message, indent=False):
    if not opts.quiet:
        if indent:
            print("   " + message)
        else:
            print(message);

##############
# logVerbose #
##############
def logVerbose(message, indent=True):
    if not opts.quiet and opts.verbose:
        if indent:
            print("   " + message)
        else:
            print(message);

################
# printElapsed #
################
def printElapsed(title, seconds):
    logVerbose("\n%s: %.03f seconds." % (title, seconds), False)

################
# getInstances #
################
def getInstances():
    logNormal("Getting SciDB configuration information.")
    cmd = "\"%siquery\" -c %s -p %d -o csv -aq \"list('instances')\"" % (sciDbBinFolder, opts.db_address, opts.db_port)
    logVerbose(cmd)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
    childProcesses.append(p)
    retVal = p.wait()
    if retVal != 0:
        err = "Failed to obtain SciDB configuration information."
        if p and p.stderr:
            err = "%s\n%s" % (err, p.stderr.read())
        raise Exception(err)
    configCsv = p.stdout.read();
    reader = csv.DictReader(cStringIO.StringIO(configCsv))
    for item in reader:
        item["name"] = item["name"].replace("'", "")
        item["csv_fragment"] = "%s_%04d" % (outputBase, int(item["instance_id"]))
        item["dlf_fragment"] = "%s/%s" % (item["instance_path"].replace("'", ""), dlfFragmentName)
        instances.append(item)
    logNormal("This SciDB installation has %d instance(s)." % len(instances))

########################
# Remove DLF Fragments #
########################
def removeDlfFragments(raiseExceptions=True):
    if (not opts.use_dlf_files) or (not opts.leave_dlf_files):
        if opts.use_dlf_files:
            logNormal("Removing DLF fragment files.")
        else:
            logNormal("Removing DLF fragment FIFOs.")

        # Start a process to remove the specified DLF fragment file/FIFO on each instance.
        tasks = []
        for instance in instances:
            cmd = "rm -f \"%s\"" % instance["dlf_fragment"]
            if instance["name"] in hostAddresses:
                logVerbose(cmd)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
            else:
                sshCmd = getSshCommand(opts.ssh_username, opts.ssh_keyfile, instance["name"], opts.ssh_port, opts.ssh_bypass_key_check, cmd)
                logVerbose(sshCmd)
                p = subprocess.Popen(sshCmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
            childProcesses.append(p)
            tasks.append({"process": p, "dlf_fragment": instance["dlf_fragment"]})

        # Wait for all of the processes to finish.
        for task in tasks:
            p = task["process"]
            retCode = p.wait()
            if retCode != 0:
                err = "Failed to remove DLF fragment: \"%s\"." % task["dlf_fragment"]
                if p and p.stderr:
                    err = "%s\n%s" % (err, p.stderr.read())
                if raiseExceptions:
                    raise Exception(err)

########################
# Create DLF Fragments #
########################
def createDlfFragments():
    if not opts.use_dlf_files:
        # Start a process to create a DLF fragment FIFO on each instance.
        logNormal("Creating DLF fragment FIFOs.")
        tasks = []
        for instance in instances:
            cmd = "mkfifo \"%s\"" % instance["dlf_fragment"]
            if instance["name"] in hostAddresses:
                logVerbose(cmd)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
            else:
                sshCmd = getSshCommand(opts.ssh_username, opts.ssh_keyfile, instance["name"], opts.ssh_port, opts.ssh_bypass_key_check, cmd)
                logVerbose(sshCmd)
                p = subprocess.Popen(sshCmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
            childProcesses.append(p)
            tasks.append({"process": p, "dlf_fragment": instance["dlf_fragment"]})

        # Wait for all of the processes to finish.
        for task in tasks:
            p = task["process"]
            retCode = p.wait()
            if retCode != 0:
                err = "Failed to create DLF fragment: \"%s\"." % task["dlf_fragment"]
                if p and p.stderr:
                    err = "%s\n%s" % (err, p.stderr.read())
                raise Exception(err)

########################
# Remove CSV Fragments #
########################
def removeCsvFragments(raiseExceptions=True):
    if (not opts.use_csv_files) or (not opts.leave_csv_files):
        if opts.use_csv_files:
            logNormal("Removing CSV fragment files.")
        else:
            logNormal("Removing CSV fragmemt FIFOs.")
        for instance in instances:
            try:
                os.remove(instance["csv_fragment"])
                logVerbose("\"%s\" removed." % instance["csv_fragment"])
            except Exception, e:
                if raiseExceptions:
                    raise e;

########################
# Create CSV Fragments #
########################
def createCsvFragments():
    if not opts.use_csv_files:
        # Create the CSV fragment FIFOs.
        logNormal("Creating CSV fragment FIFOs.")
        for instance in instances:
            os.mkfifo(instance["csv_fragment"])
            logVerbose("\"%s\" created." % instance["csv_fragment"])

#########
# split #
#########
def split():
    logNormal("Starting CSV splitting process.")

    # Need a new pipe if inputFile is in-memory StringIO buffer.
    stringio_input = isinstance(inputFile, pyStringIO.StringIO)
    stdin = subprocess.PIPE if stringio_input else inputFile

    # Split the input file.
    rawCmd = [ ''.join((sciDbBinFolder, 'osplitcsv')),
               '-n', len(instances),
               '-c', opts.chunk_size,
               '-s', opts.skip,
               '-o', outputBase
               ]
    if opts.delimiter:
        rawCmd.extend(['-d', opts.delimiter])
    if opts.type_pattern:
        rawCmd.extend(['-t', opts.type_pattern])
    if not os.getenv("SCIDB_USE_CSV"):
        # With the new conversion program, prefer TSV intermediate format.
        rawCmd.append('--format=tsv')
    cmd = map(str, rawCmd)
    logVerbose(' '.join(cmd))
    p = subprocess.Popen(cmd, stdin=stdin, stdout=devNull,
                         stderr=sys.stderr, close_fds=True,
                         preexec_fn=os.setsid)
    childProcesses.append(p)

    # If we made a new pipe, we need to feed it!  (Since stdout above
    # is /dev/null we need not worry about deadlocks.)
    if stringio_input:
        nbytes = 0
        for line in inputFile:
            nbytes += len(line)
            p.stdin.write(line)
        p.stdin.close()
        logVerbose("Shoved %d bytes down splitcsv pipe" % nbytes)

    # If we are using files, wait until the split is complete.
    if opts.use_csv_files:
        retCode = p.wait()
        if retCode != 0:
            err = "Failed to split input CSV file."
            raise Exception(err)

########################
# distributeAndConvert #
########################
def distributeAndConvert():
    logNormal("Starting CSV distribution and conversion processes.")
    tasks = []
    startingCoordinate = opts.starting_coordinate
    if os.getenv("SCIDB_USE_CSV"):
        # Old conversion program, if you insist.
        converter = "csv2scidb"
        local_delim = opts.delimiter if opts.delimiter else ','
    else:
        converter = "tsv2scidb"
        local_delim = "\\t" # because we used splitcsv --format=tsv option
    logNormal("Converter is %s" % converter)
    for instance in instances:
        cmd = "\"%s%s\"" % (sciDbBinFolder, converter)
        if opts.type_pattern:
            cmd = "%s -p \"%s\"" % (cmd, opts.type_pattern)
        cmd = "%s -c %d -f %d -n %d -d \"%s\" -o \"%s\"" % (
            cmd, opts.chunk_size, startingCoordinate, len(instances),
            local_delim, instance["dlf_fragment"])
        if instance["name"] in hostAddresses:
            pipedCmd = "cat \"%s\" | %s" % (instance["csv_fragment"], cmd)
        else:
            sshCmd = getSshCommand(opts.ssh_username, opts.ssh_keyfile,
                                   instance["name"], opts.ssh_port, opts.ssh_bypass_key_check, cmd)
            pipedCmd = "cat \"%s\" | %s" % (instance["csv_fragment"], sshCmd)
        logVerbose(pipedCmd)
        p = subprocess.Popen(pipedCmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=sys.stderr, shell=True, close_fds=True, preexec_fn=os.setsid)
        childProcesses.append(p)
        tasks.append({"process": p, "csv_fragment": instance["csv_fragment"]})
        startingCoordinate += opts.chunk_size
    if opts.use_dlf_files:
        # If we are using files, wait until they are converted.
        for task in tasks:
            p = task["process"]
            retCode = p.wait()
            if retCode != 0:
                err = "Failed to distribute and convert the CSV fragment: \"%s\"." % (task["csv_fragment"])
                if p and p.stderr:
                    err = "%s\n%s" % (err, p.stderr.read())
                raise Exception(err)

###############
# createArray #
###############
def createArray(arrayName, arraySchema):
    logNormal("Creating \"%s\" array." % arrayName)
    cmd = "\"%siquery\" -c %s -p %d -nq \"CREATE ARRAY %s %s\"" % (sciDbBinFolder, opts.db_address, opts.db_port, arrayName, arraySchema)
    logVerbose(cmd)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
    childProcesses.append(p)
    retCode = p.wait()
    if retCode != 0:
        err = "Failed to create array: \"%s\"." % arrayName
        if p and p.stderr:
            err = "%s\n%s" % (err, p.stderr.read())
        raise Exception(err)

###############
# removeArray #
###############
def removeArray(arrayName, raiseExceptions=True):
    if arrayName:
        logNormal("Removing \"%s\" array." % arrayName)
        cmd = "\"%siquery\" -c %s -p %d -anq \"remove(%s)\"" % (sciDbBinFolder, opts.db_address, opts.db_port, arrayName)
        logVerbose(cmd)
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
        childProcesses.append(p)
        retCode = p.wait()
        if retCode != 0:
            err = "Failed to remove array: \"%s\"." % arrayName
            if p and p.stderr:
                err = "%s\n%s" % (err, p.stderr.read())
            if raiseExceptions:
                raise Exception(err)

########
# load #
########
def load():
    if opts.load_name and opts.load_schema:
        # Remove the load and shadow arrays before loading.
        if opts.remove_load_arrays:
            removeArray(opts.load_name, False)
            removeArray(opts.shadow_name, False)

        # Create a new array using the provided name and schema.
        createArray(opts.load_name, opts.load_schema)

    # Target Array
    if opts.target_name:
        if opts.load_name or opts.load_schema:
            if opts.target_schema:
                # Remove the target array.
                if opts.remove_target_array:
                    removeArray(opts.target_name, False)

                # Create a new array using the provided name and schema.
                createArray(opts.target_name, opts.target_schema)

            # Transform from load to target.
            if opts.transform == "RSL":
                # redimension_store(load).
                logNormal("Loading data into \"%s\" array using redimension_store of load (may take a while for large input files)." % opts.target_name)
                if opts.load_name:
                    # Load array name was provided.
                    loadCmd = "load(%s, '%s', -1, 'text', %d" % (opts.load_name, dlfFragmentName, opts.errors_allowed)
                else:
                    # We are using an anonymous load schema.
                    loadCmd = "load(%s, '%s', -1, 'text', %d" % (opts.load_schema, dlfFragmentName, opts.errors_allowed)
                # Shadow array, if specified.
                if opts.shadow_name:
                    loadCmd += ", %s)" % opts.shadow_name
                else:
                    loadCmd += ")"
                redimCmd = "store( redimension(%s, %s), %s)" % (loadCmd, opts.target_name, opts.target_name)
                cmd = "\"%siquery\" -c %s -p %d -anq \"%s\"" % (sciDbBinFolder, opts.db_address, opts.db_port, redimCmd)
                logVerbose(cmd)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
                childProcesses.append(p)
                retCode = p.wait()
                if retCode != 0:
                    err = "Load failed."
                    if p and p.stderr:
                        err += "\n" + p.stderr.read()
                    raise Exception(err)
            elif opts.transform == "RSI":
                # Using redimension_store(input).
                logNormal("Loading data into \"%s\" array using redimension_store of input (may take a while for large input files)." % opts.target_name)
                if opts.load_name:
                    # Load array name was provided.
                    inputCmd = "input(%s, '%s', -1, 'text', %d" % (opts.load_name, dlfFragmentName, opts.errors_allowed)
                else:
                    # We are using an anonymous load schema.
                    inputCmd = "input(%s, '%s', -1, 'text', %d" % (opts.load_schema, dlfFragmentName, opts.errors_allowed)
                # Shadow array, if specified.
                if opts.shadow_name:
                    inputCmd += ", %s)" % opts.shadow_name
                else:
                    inputCmd += ")"
                redimCmd = "store( redimension(%s, %s), %s)" % (inputCmd, opts.target_name, opts.target_name)
                cmd = "\"%siquery\" -c %s -p %d -anq \"%s\"" % (sciDbBinFolder, opts.db_address, opts.db_port, redimCmd)
                logVerbose(cmd)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
                childProcesses.append(p)
                retCode = p.wait()
                if retCode != 0:
                    err = "Load failed."
                    if p and p.stderr:
                        err += "\n" + p.stderr.read()
                    raise Exception(err)
            elif opts.transform == "IRL":
                # insert(redimension(load))
                logNormal("Loading data into \"%s\" array using insert of redimension of load (may take a while for large input files)." % opts.target_name)
                if opts.load_name:
                    # Load array name was provided.
                    loadCmd = "load(%s, '%s', -1, 'text', %d" % (opts.load_name, dlfFragmentName, opts.errors_allowed)
                else:
                    # We are using an anonymous load schema.
                    loadCmd = "load(%s, '%s', -1, 'text', %d" % (opts.load_schema, dlfFragmentName, opts.errors_allowed)
                if opts.shadow_name:
                    loadCmd += ", %s)" % opts.shadow_name
                else:
                    loadCmd += ")"
                redimCmd = "redimension(%s, %s)" % (loadCmd, opts.target_name)
                insertCmd = "insert(%s, %s)" % (redimCmd, opts.target_name)
                cmd = "\"%siquery\" -c %s -p %d -anq \"%s\"" % (sciDbBinFolder, opts.db_address, opts.db_port, insertCmd)
                logVerbose(cmd)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
                childProcesses.append(p)
                retCode = p.wait()
                if retCode != 0:
                    err = "Load failed."
                    if p and p.stderr:
                        err += "\n" + p.stderr.read()
                    raise Exception(err)
            elif opts.transform == "IRI":
                # insert(redimension(input))
                logNormal("Loading data into \"%s\" array using insert of redimension of input (may take a while for large input files)." % opts.target_name)
                if opts.load_name:
                    # Load array name was provided.
                    inputCmd = "input(%s, '%s', -1, 'text', %d" % (opts.load_name, dlfFragmentName, opts.errors_allowed)
                else:
                    # We are using an anonymous load schema.
                    inputCmd = "input(%s, '%s', -1, 'text', %d" % (opts.load_schema, dlfFragmentName, opts.errors_allowed)
                if opts.shadow_name:
                    inputCmd += ", %s)" % opts.shadow_name
                else:
                    inputCmd += ")"
                redimCmd = "redimension(%s, %s)" % (inputCmd, opts.target_name)
                insertCmd = "insert(%s, %s)" % (redimCmd, opts.target_name)
                cmd = "\"%siquery\" -c %s -p %d -anq \"%s\"" % (sciDbBinFolder, opts.db_address, opts.db_port, insertCmd)
                logVerbose(cmd)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
                childProcesses.append(p)
                retCode = p.wait()
                if retCode != 0:
                    err = "Load failed."
                    if p and p.stderr:
                        err += "\n" + p.stderr.read()
                    raise Exception(err)
        else:
            raise Exception("When specifying a target array name, a load array name and/or load array schema must also be provided.")
    else:
        if opts.load_name:
            # We are only going to load (no re-dimensioning).
            logNormal("Loading data into \"%s\" array (may take a while for large input files). 1-D load only since no target array name was provided." % opts.load_name)
            cmd = "\"%siquery\" -c %s -p %d -anq \"load(%s, '%s', -1, 'text', %d" % (sciDbBinFolder, opts.db_address, opts.db_port, opts.load_name, dlfFragmentName, opts.errors_allowed)
            if opts.shadow_name:
                cmd = "%s, %s)\"" % (cmd, opts.shadow_name)
            else:
                cmd = "%s)\"" % cmd
            logVerbose(cmd)
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, close_fds=True, preexec_fn=os.setsid)
            childProcesses.append(p)
            retCode = p.wait()
            if retCode != 0:
                err = "Load failed."
                if p and p.stderr:
                    err = "%s\n%s" % (err, p.stderr.read())
                raise Exception(err)


def main(argv=None):
    if argv is None:
        argv = sys.argv

    #############################
    # Get command line options. #
    #############################

    # Note that changes here may need to be reflected in
    # loadpipe.py's DISALLOWED_LOADCSV_OPTIONS list.
    parser = optparse.OptionParser(description="SciDB Parallel CSV Loader")
    parser.add_option("-d", help="SciDB Coordinator Hostname or IP Address (Default = \"localhost\")", action="store", dest="db_address", default="localhost")
    parser.add_option("-p", help="SciDB Coordinator Port (Default = 1239)", action="store", dest="db_port", type=int, default=1239)
    parser.add_option("-r", help="SciDB Installation Root Folder (Default = \"/opt/scidb/14.8\")", action="store", dest="db_root", default="/opt/scidb/14.8")
    parser.add_option("-i", help="CSV Input File (Default = stdin)", action="store", dest="input_file")
    parser.add_option("-n", help="# Lines to Skip (Default = 0)", action="store", dest="skip", type=int, default=0)
    parser.add_option("-t", action="store", dest="type_pattern",
                      help=' '.join(("CSV Field Types Pattern: N number, S string, s nullable-string, C char",
                                    "(e.g., \"NNsCS\").  Use of this flag is deprecated.")))
    parser.add_option("-D", help="Delimiter (Default = guessed from input)", action="store", dest="delimiter",
                      default=None)
    parser.add_option("-f", help="Starting Coordinate (Default = 0)", action="store", dest="starting_coordinate", type=long, default=0)
    parser.add_option("-c", help="Chunk Size (Default = 500000)", action="store", dest="chunk_size", type=long, default=500000)
    parser.add_option("-o", help="Output File Base Name (Default = INPUT_FILE or \"stdin.csv\")", action="store", dest="output_base")
    parser.add_option("-m", help="Create Intermediate CSV Files (not FIFOs)", action="store_true", dest="use_csv_files")
    parser.add_option("-l", help="Leave Intermediate CSV Files", action="store_true", dest="leave_csv_files")
    parser.add_option("-M", help="Create Intermediate DLF Files (not FIFOs)", action="store_true", dest="use_dlf_files")
    parser.add_option("-L", help="Leave Intermediate DLF Files", action="store_true", dest="leave_dlf_files")
    parser.add_option("-P", help="SSH Port (Default = System Default)", action="store", dest="ssh_port", type=int)
    parser.add_option("-u", help="SSH Username", action="store", dest="ssh_username")
    parser.add_option("-k", help="SSH Key/Identity File", action="store", dest="ssh_keyfile")
    parser.add_option("-b", help="SSH Bypass Strict Host Key Checking", action="store_true", dest="ssh_bypass_key_check")
    parser.add_option("-a", help="Load Array Name", action="store", dest="load_name")
    parser.add_option("-s", help="Load Array Schema", action="store", dest="load_schema")
    parser.add_option("-w", help="Shadow Array Name", action="store", dest="shadow_name")
    parser.add_option("-e", help="# Load Errors Allowed per Instance (Default = 0)", action="store", dest="errors_allowed", type=int, default=0)
    parser.add_option("-x", help="Remove Load and Shadow Arrays Before Loading (if they exist)", action="store_true", dest="remove_load_arrays")
    parser.add_option("-A", help="Target Array Name", action="store", dest="target_name")
    parser.add_option("-S", help="Target Array Schema", action="store", dest="target_schema")
    parser.add_option("-T", help="Directory for temporary files", action="store", dest="temp_dir")
    parser.add_option("-X", help="Remove Target Array Before Loading (if it exists)", action="store_true", dest="remove_target_array")
    parser.add_option("-z", help=optparse.SUPPRESS_HELP, action="store", type="choice", choices=["RSL", "RSI", "IRL", "IRI"], default="RSL", dest="transform")
    parser.add_option("-v", help="Display Verbose Messages", action="store_true", dest="verbose")
    parser.add_option("-V", help="Display SciDB Version Information", action="store_true", dest="show_version")
    parser.add_option("-q", help="Quiet Mode", action="store_true", dest="quiet")

    global opts, inputFile
    (opts, args) = parser.parse_args(argv[1:])
    setupModuleVariables()

    # Specifying "~/" in path can cause issues. Expanding it out takes care of them.
    wantClose = False
    if opts.input_file and isinstance(opts.input_file, basestring):
        opts.input_file = os.path.expanduser(opts.input_file)
        wantClose = True
    if opts.output_base:
        opts.output_base = os.path.expanduser(opts.output_base)

    #############################
    # Coordinate all operations #
    #############################
    dataLoaded = False
    exceptionEncountered = False
    csvFragmentsCreated = False
    dlfFragmentsCreated = False
    exitStatus = 0
    try:
        try:
            start = time.time()
            showVersion()
            getLoadSchema()
            setChunkSize()
            setTypePattern()
            if opts.input_file:
                if isinstance(opts.input_file, basestring):
                    inputFile = file(opts.input_file)
                else:
                    # It's a StringIO passed from a calling module.
                    inputFile = opts.input_file
            if isinstance(inputFile, pyStringIO.StringIO) \
                    or select.select([inputFile],[],[])[0]:
                getInstances()
                createCsvFragments()
                csvFragmentsCreated = True
                createDlfFragments()
                dlfFragmentsCreated = True
                split()
                distributeAndConvert()
                load()
                dataLoaded = True
            else:
                print("Warning: No input data was found.")
        except Exception, e:
            exceptionEncountered = True
            print("\n##### ERROR ##################")
            print(e)
            #traceback.print_exc()
            print("##############################\n")
    finally:
        logVerbose("Performing cleanup tasks.", False)
        if inputFile is not sys.stdin and wantClose:
            inputFile.close()

        # Kill any child processes that might be running... just in case.
        for p in childProcesses:
            retCode = p.poll()
            if retCode == None:
                logVerbose("Terminating child process with pid = %d." % p.pid)
                os.killpg(p.pid, signal.SIGTERM)
                p.wait()

        # Remove temporary fragment files/FIFOs.
        if csvFragmentsCreated:
            removeCsvFragments(False)
        if dlfFragmentsCreated:
            removeDlfFragments(False)
        shutil.rmtree(tmpDir, ignore_errors=True)

        # Calculate total elapsed time.
        stop = time.time()
        totalTime = stop - start

        #################
        # Report Status #
        #################
        # Print the total time.
        printElapsed("Total Elapsed Time", totalTime)

        # Status
        if exceptionEncountered:
            logNormal("Failure: Error Encountered.\n")
            exitStatus = 2
        else:
            if dataLoaded:
                logNormal("Success: Data Loaded.\n")
            else:
                logNormal("No Data Loaded.\n")
                exitStatus = 1

    return exitStatus


if __name__ == '__main__':
    sys.exit(main())
