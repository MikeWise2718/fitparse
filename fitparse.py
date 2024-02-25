import fitparse
import argparse
import os
import time


def get_args():
    parser = argparse.ArgumentParser(description="Dump Fit Records",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # General Settings
    parser.add_argument("-v", "--verbosity", action='store', type=int, default=2,
                        help="0=Errors, 1=Quiet, 2=Normal, 3=Info, 4=Debug")
    parser.add_argument("-a", "--action", action='store',
                        required=False, help="list/vo2max/dumptypes or l/va/dt", default="list")

    # Input Files
    parser.add_argument("-ff", "--fitfile", action='store',
                        required=False, help="fit file name", default="tests/files/2013-02-06-12-11-14.fit")
    parser.add_argument("-fd", "--fitdir", action='store',
                        required=False, help="directory holding fit files", default="")
    parser.add_argument("-rf", "--recordfilter", action='store',
                        required=False, help="record filter for list by type (examine types with action dt)", default="")



    args = parser.parse_args()
    return args


# https://forum.intervals.icu/t/garmin-vo2-max-from-fit-file/24268/10


def dump_types(fitfile, verb=2):

        print(f"--- {fitfile} ---")
        fitparsed = fitparse.FitFile(fitfile)
        counts = {}

        for record in fitparsed.get_messages():
            counts[record.name] = counts.get(record.name, 0) + 1

        for key in counts:
            print(f"     {key}: {counts[key]}")

def dump_records(fitfile, filz=None, verb=2):

    fitparsed = fitparse.FitFile(fitfile)

    if filz=="" or filz==None:
        filz = None

    for record in fitparsed.get_messages(filz):
        print(f"--- {record.name} start ---")

        # Records can contain multiple pieces of data (ex: timestamp, latitude, longitude, etc)
        for data in record:

            # Print the name and value of the data (and the units if it has any)
            if data.units:
                # print(" * {}: {} ({})".format(data.name, data.value, data.units))
                print(f" * {data.name}: {data.value} ({data.units})")
            else:
                # print(" * {}: {}".format(data.name, data.value))
                print(f" * {data.name}: {data.value}")

        print(f"---  end of record {record.name}")


def dump_vo2max(fitfile, verb=2):

    fitparsed = fitparse.FitFile(fitfile)

    sport = ""
    subsport = ""
    for record in fitparsed.get_messages("sport"):
        vals = record.get_values()
        if "sport" in vals:
            sport = vals['sport']
        if "subsport" in vals:
            subsport = vals['subsport']

    icnt = 0
    for record in fitparsed.get_messages("event"):
        date = record.get_values()['timestamp']
        if icnt==0:
            dmin = date
            dmax = date
        else:
            if date < dmin:
                dmin = date
            if date > dmax:
                dmax = date
        icnt += 1

    dsamp = icnt

    icnt = 0
    vo2_max_min = 0
    vo2_max_max = 0
    for record in fitparsed.get_messages("unknown_140"):
        vo2_max = round(record.get_values()['unknown_7'] * 3.5 / 65536, 2)
        if icnt==0:
            vo2_max_min = vo2_max
            vo2_max_max = vo2_max
        else:
            if vo2_max < vo2_max_min:
                vo2_max_min = vo2_max
            if vo2_max > vo2_max_max:
                vo2_max_max = vo2_max
            if verb>2:
                print(f" vo2_max_{icnt}: {vo2_max}")
        icnt += 1
    vsamp = icnt

    print(f" {sport} {subsport} {dmin}-{dmax} ({dsamp}): {vo2_max_min}-{vo2_max_max}   ({vsamp})  {fitfile}")

def dump_types_dir(fitdir, verb=2):

    files = os.listdir(fitdir)

    for file in files:
        if file.endswith(".fit"):
            fitfile = fitdir + "/" + file
            dump_types(fitfile, verb=verb)


def dump_vo2max_dir(fitdir, verb=2):

    files = os.listdir(fitdir)

    for file in files:
        if file.endswith(".fit"):
            fitfile = fitdir + "/" + file
            dump_vo2max(fitfile, verb=verb)


def dump_records_dir(fitdir,filz, verb=2):

    files = os.listdir(fitdir)

    for file in files:
        if file.endswith(".fit"):
            fitfile = fitdir + "/" + file
            dump_records(fitfile,filz, verb=verb)

def main():
    args = get_args()
    fitfile_name = args.fitfile
    fitdir_name = args.fitdir
    # Load the FIT file
    action = args.action
    verb = args.verbosity
    filz = args.recordfilter

    if action=="dt":
        action = "dumptypes"
    elif action=="l":
        action = "list"
    elif action=="v":
        action = "vo2max"

    if args.fitdir != "":
        match action:
            case "dumptypes":
                dump_types_dir(fitdir_name,verb=verb)
            case "list":
                dump_records_dir(fitdir_name,filz,verb=verb)
            case "vo2max":
                dump_vo2max_dir(fitdir_name,verb=verb)
            case _:
                print("Unknown action: ", action)
    else:
        match action:
            case "dumptypes":
                dump_records(fitfile_name,verb=verb)
            case "list":
                dump_records(fitfile_name,verb=verb)
            case "list":
                dump_records(fitfile_name,verb=verb)
            case "vo2max":
                dump_vo2max(fitfile_name,verb=verb)
            case _:
                print("Unknown action: ", action)

    # Iterate over all messages of type "record"
    # (other types include "device_info", "file_creator", "event", etc)

if __name__ == "__main__":
    main()
