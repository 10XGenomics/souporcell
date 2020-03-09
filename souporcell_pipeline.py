#!/usr/bin/env python

import argparse

import logging
logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%I:%M:%S %p')
logging.warning('Starting pipeline')

def print(msg):
    logging.warning(msg)

parser = argparse.ArgumentParser(
    description="single cell RNAseq mixed genotype clustering using sparse mixture model clustering with tensorflow.")
parser.add_argument("-i", "--bam", required = True, help = "cellranger bam")
parser.add_argument("-b", "--barcodes", required = True, help = "barcodes.tsv from cellranger")
parser.add_argument("-f", "--fasta", required = True, help = "reference fasta file")
parser.add_argument("-t", "--threads", required = True, type = int, help = "max threads to use")
parser.add_argument("-o", "--out_dir", required = True, help = "name of directory to place souporcell files")
parser.add_argument("-k", "--clusters", required = True, help = "number cluster, tbd add easy way to run on a range of k")
parser.add_argument("-p", "--ploidy", required = False, default = "2", help = "ploidy, must be 1 or 2, default = 2")
parser.add_argument("--min_alt", required = False, default = "4", help = "min alt to use locus, default = 10.")
parser.add_argument("--min_ref", required = False, default = "4", help = "min ref to use locus, default = 10.")
parser.add_argument("--max_loci", required = False, default = "2048", help = "max loci per cell, affects speed, default = 2048.")
parser.add_argument("--restarts", required = False, default = 100, type = int, 
    help = "number of restarts in clustering, when there are > 12 clusters we recommend increasing this to avoid local minima")
parser.add_argument("--common_variants", required = False, default = None, 
    help = "common variant loci or known variant loci vcf, must be vs same reference fasta")
parser.add_argument("--known_genotypes", required = False, default = None, 
    help = "known variants per clone in population vcf mode, must be .vcf right now we dont accept gzip or bcf sorry")
parser.add_argument("--known_genotypes_sample_names", required = False, nargs = '+', default = None, 
    help = "which samples in population vcf from known genotypes option represent the donors in your sample")
parser.add_argument("--skip_remap", required = False, default = False, type = bool, 
    help = "don't remap with minimap2 (not recommended unless in conjunction with --common_variants")
parser.add_argument("--ignore", required = False, default = "False", help = "set to True to ignore data error assertions")
args = parser.parse_args()

print("checking modules")
# importing all reqs to make sure things are installed
print("importing numpy")
import numpy as np
print("importing scipy")
import scipy
print("importing gzip")
import gzip
print("importing math")
import math
print("importing pystan")
import pystan
print("importing pyvcf")
import vcf
print("importing pysam")
import pysam

print("importing pyfaidx")
import pyfaidx

print("importing subprocess")
import subprocess

print("importing time")
import time

print("importing os")
import os
print("imports done")

print("checking bam for expected tags")
#load each file to make sure it is legit
bc_set = set()
with open(args.barcodes) as barcodes:
    for (index, line) in enumerate(barcodes):
        bc = line.strip()
        bc_set.add(bc)

assert len(bc_set) > 50, "Fewer than 50 barcodes in barcodes file? We expect 1 barcode per line."

assert not(not(args.known_genotypes == None) and not(args.common_variants == None)), "cannot set both know_genotypes and common_variants"
if args.known_genotypes_sample_names:
    assert not(args.known_genotypes == None), "if you specify known_genotype_sample_names, must specify known_genotypes option"
    assert len(args.known_genotypes_sample_names) == int(args.clusters), "length of known genotype sample names should be equal to k/clusters"
if args.known_genotypes:
    reader = vcf.Reader(open(args.known_genotypes))
    assert len(reader.samples) >= int(args.clusters), "number of samples in known genotype vcfs is less than k/clusters"
    if args.known_genotypes_sample_names == None:
        args.known_genotypes_sample_names = reader.samples
    for sample in args.known_genotypes_sample_names:
        assert sample in args.known_genotypes_sample_names, "not all samples in known genotype sample names option are in the known genotype samples vcf?"

    

#test bam load
bam = pysam.AlignmentFile(args.bam)
num_cb = 0
num_cb_cb = 0 # num reads with barcodes from barcodes.tsv file
num_umi = 0
num_read_test = 100000
for (index,read) in enumerate(bam):
    if index >= num_read_test:
        break
    if read.has_tag("CB"):
        num_cb += 1
        if read.get_tag("CB") in bc_set:
            num_cb_cb += 1
    if read.has_tag("UB"):
        num_umi += 1
if not args.ignore == "True":
    if args.skip_remap and args.common_variants == None and args.known_genotypes == None:
        assert False, "WARNING: skip_remap enables without common_variants or known genotypes. Variant calls will be of poorer quality. Turn on --ignore True to ignore this warning"
        
    assert float(num_cb) / float(num_read_test) > 0.5, "Less than 50% of first 100000 reads have cell barcode tag (CB), turn on --ignore True to ignore"
    assert float(num_umi) / float(num_read_test) > 0.5, "Less than 50% of first 100000 reads have UMI tag (UB), turn on --ignore True to ignore"
    assert float(num_cb_cb) / float(num_read_test) > 0.05, "Less than 25% of first 100000 reads have cell barcodes from barcodes file, is this the correct barcode file? turn on --ignore True to ignore"

print("checking fasta")
fasta = pyfaidx.Fasta(args.fasta, key_function = lambda key: key.split()[0])

def make_fastqs(args):
    if not os.path.isfile(args.bam + ".bai"):
        print("no bam index found, creating")
        subprocess.check_call(['samtools', 'index', args.bam])
    if not os.path.isfile(args.fasta + ".fai"):
        print("fasta index not found, creating")
        subprocess.check_call(['samtools', 'faidx', args.fasta])
    bam = pysam.AlignmentFile(args.bam)
    total_reference_length = 0
    for chrom in bam.references:
        total_reference_length += bam.get_reference_length(chrom)
    step_length = int(math.ceil(total_reference_length / (2 * int(args.threads))))

    region = []

    print("creating chunks")
    for chrom in bam.references:
        chrom_length = bam.get_reference_length(chrom)
        for i in range(math.ceil(chrom_length / step_length)):
            start = step_length * i
            end = min(chrom_length, start + step_length)
            region.append((chrom, start, end))

    # for testing, delete this later
    args.threads = int(args.threads)
    all_fastqs = []
    procs = [None for x in range(args.threads)]
    any_running = True
    # run renamer in parallel manner

    current_region = 0
    print("generating fastqs with cell barcodes and umis in readname")
    while any_running:
        any_running = False

        for index in range(len(procs)):
            slot_open = True
            if procs[index]:
                returncode = procs[index].poll()
                slot_open = returncode is not None
                if not slot_open:
                    any_running = True
                else:
                    assert returncode == 0, "renamer subprocess terminated abnormally with code " + str(returncode)

            if slot_open and current_region < len(region):
                (chrom, start, end) = region[current_region]
                fq_name = args.out_dir + "/souporcell_fastq_" + str(current_region) + ".fq"
                p = subprocess.Popen(["renamer.py", "--bam", args.bam, "--barcodes", args.barcodes, "--out", fq_name,
                        "--chrom", chrom, "--start", str(start), "--end", str(end)])
                all_fastqs.append(fq_name)
                procs[index] = p
                any_running = True
                current_region += 1

        if not any_running and current_region == len(region):
            break

        time.sleep(0.5)

    print("done writing fastqs")

    with open(args.out_dir + "/fastqs.done", 'w') as done:
        for fastq in all_fastqs:
            done.write(fastq + "\n")
    return all_fastqs

def remap(args, all_fastqs):
    # run minimap2

    print("indexing genome for minimap2")
    minimap_index = args.out_dir + "/minimap_index.mmi"
    subprocess.check_call(["minimap2", "-ax", "splice", "-sr", "-k", "21", "-w", "11", "-d", minimap_index, args.fasta])

    #merged_fq_fn = args.out_dir + "/merged.fastq"
    #with open(merged_fq_fn, "w") as merged_fq:
    #    subprocess.check_call(["cat"] + all_fastqs, stdout = merged_fq)

    fifo = args.out_dir + "/fasta.fifo"
    subprocess.check_call(["mkfifo", fifo])

    # Stream data to fifo
    fifo_proc = subprocess.Popen("cat " + " ".join(all_fastqs) + " > " + fifo, shell=True)


    samtools_threads = 3

    print("remapping with minimap2")
    with open(args.out_dir + "/minimap.err",'w') as minierr:
        minierr.write("mapping\n")
        cmd = ["minimap2", "-ax", "splice", "-t", str(args.threads), "-G50k", "-k", "21",
               "-w", "11", "--sr", "-A2", "-B8", "-O12,32", "-E2,1", "-r200", "-p.5", "-N20", "-f1000,5000",
               "-y", "-n2", "-m20", "-s40", "-g2000", "-2K50m", "--secondary=no", minimap_index, fifo]
        minierr.write(" ".join(cmd)+"\n")
        minimap_ps = subprocess.Popen(cmd, stdout = subprocess.PIPE, stderr = minierr)
        output = args.out_dir + "/souporcell_minimap.bam"
        subprocess.check_call(["samtools", "view", "-b", "-@", str(samtools_threads), "-o", output, "-"], stdin=minimap_ps.stdout, stderr = minierr)

    with open(args.out_dir + '/remapping.done', 'w') as done:
            done.write(output + "\n")

    # clean up tmp files
    for fq in all_fastqs:
        subprocess.check_call(["rm", fq])
    subprocess.check_call(["rm", fifo])
    subprocess.check_call(["rm", minimap_index])

    return output

def sort(args, minimap_tmp_file):

    print("sorting bam")
    final_bam = args.out_dir + "/souporcell_minimap_tagged_sorted.bam"

    subprocess.check_call(["samtools", "sort", "-@", str(args.threads), "--write-index", "-o", final_bam,  minimap_tmp_file])

    # clean up tmp bams
    subprocess.check_call(['rm', minimap_tmp_file])
    subprocess.check_call(["touch", args.out_dir + "/retagging.done"])

    return final_bam

def freebayes(args, bam, fasta):
    total_reference_length = 0
    for chrom in sorted(fasta.keys()):
       total_reference_length += len(fasta[chrom])
    step_length = int(math.ceil(total_reference_length/int(args.threads)))
    if not(args.common_variants == None) or not(args.known_genotypes == None):
        if not(args.common_variants == None):
            print("using common variants")
        if not(args.known_genotypes == None):
            print("using known genotypes")
            args.common_variants = args.known_genotypes
        with open(args.out_dir+"/depth.bed", 'w') as bed:
            ps = subprocess.Popen(['samtools', 'depth', bam], stdout = subprocess.PIPE)
            min_cov = int(args.min_ref)+int(args.min_alt)
            #magic
            subprocess.check_call(["awk '{ if ($3 >= " + str(min_cov) + " && $3 < 100000) { print $1 \"\t\" $2 \"\t\" $2+1 \"\t\" $3 } }'"], 
                shell = True, stdin = ps.stdout, stdout = bed)
        with open(args.out_dir + "/depth_merged.bed", 'w') as bed:
            subprocess.check_call(["bedtools", "merge", "-i", args.out_dir + "/depth.bed"], stdout = bed)
        with open(args.out_dir + "/common_variants_covered_tmp.vcf", 'w') as vcf:
            subprocess.check_call(["bedtools", "intersect", "-wa", "-a", args.common_variants, "-b", args.out_dir + "/depth_merged.bed"], stdout = vcf)
        with open(args.out_dir + "/common_variants_covered_tmp.vcf") as vcf:
            with open(args.common_variants) as common:
                with open(args.out_dir + "/common_variants_covered.vcf",'w') as out:
                    for line in common:
                        if line.startswith("#"):
                            out.write(line)
                        else:
                            break
                    for line in vcf:
                        out.write(line)
        with open(args.out_dir + "/variants.done", 'w') as done:
            done.write(args.out_dir + "/common_variants_covered.vcf" + "\n")
        return(args.out_dir + "/common_variants_covered.vcf")

    regions = []
    region = []
    region_so_far = 0
    chrom_so_far = 0
    for chrom in sorted(fasta.keys()):
        chrom_length = len(fasta[chrom])
        if chrom_length < 250000:
            continue
        while True:
            if region_so_far + (chrom_length - chrom_so_far) < step_length:
                region.append((chrom, chrom_so_far, chrom_length))
                region_so_far += chrom_length - chrom_so_far
                chrom_so_far = 0
                break
            else:
                region.append((chrom, chrom_so_far, step_length - region_so_far))
                regions.append(region)
                region = []
                chrom_so_far += step_length - region_so_far + 1
                region_so_far = 0
    if len(region) > 0:
        if len(regions) == args.threads:
            regions[-1] = regions[-1] + region
        else:
            regions.append(region)

    region_vcfs = [[] for x in range(args.threads)]
    all_vcfs = []
    bed_files = []
    procs = [None for x in range(args.threads)]
    any_running = True
    filehandles = []
    errhandles = []
    # run renamer in parallel manner
    print("running freebayes")
    while any_running:
        any_running = False
        for (index, region) in enumerate(regions):
            block = False
            if procs[index]:
                block = procs[index].poll() == None
                if block:
                    any_running = True
                else:
                    assert not(procs[index].returncode), "freebayes subprocess terminated abnormally with code " + str(procs[index].returncode)
            if len(region_vcfs[index]) == len(region) - 1:
                block = True
            if not block:
                sub_index = len(region_vcfs[index])
                chrom = region[sub_index][0]
                start = region[sub_index][1]
                end = region[sub_index][2]
                vcf_name = args.out_dir + "/souporcell_" + str(index) + "_" + str(sub_index) + ".vcf"
                filehandle = open(vcf_name, 'w')
                filehandles.append(filehandle)
                errhandle = open(vcf_name + ".err", 'w')
                errhandles.append(errhandle)
                    
                cmd = ["freebayes", "-f", args.fasta, "-iXu", "-C", "2",
                    "-q", "20", "-n", "3", "-E", "1", "-m", "30", 
                    "--min-coverage", str(args.min_alt+args.min_ref), "--pooled-continuous", "--skip-coverage", "100000"]
                cmd.extend(["-r", chrom + ":" + str(start) + "-" + str(end)])
                cmd.append(bam)
                errhandle.write(" ".join(cmd) + "\n")
                p = subprocess.Popen(cmd, stdout = filehandle, stderr = errhandle)
                all_vcfs.append(vcf_name)
                procs[index] = p
                region_vcfs[index].append(vcf_name)
                any_running = True
        time.sleep(1)
    for filehandle in filehandles:
        filehandle.close()
    for errhandle in errhandles:
        errhandle.close()
    print("merging vcfs")
    with open(args.out_dir + "/souporcell_merged_vcf.vcf", 'w') as vcfout:
        subprocess.check_call(["bcftools", "concat"] + all_vcfs, stdout = vcfout)
    with open(args.out_dir + "/bcftools.err", 'w') as vcferr:
        with open(args.out_dir + "/souporcell_merged_sorted_vcf.vcf", 'w') as vcfout:
            subprocess.check_call(['bcftools', 'sort', args.out_dir + "/souporcell_merged_vcf.vcf"], stdout = vcfout, stderr = vcferr)
    if not args.common_variants == None:
        with open(args.out_dir + "/common.err", 'w') as err:
            with open(args.out_dir + "/vcftmp", 'w') as out:
                subprocess.check_call(['bedtools', 'intersect', '-wa', 
                    '-a', args.out_dir + "/souporcell_merged_vcf.vcf", '-b', args.common_variants], stdout = out, stderr = err)
        subprocess.check_call(['mv', args.out_dir + "/vcftmp", args.out_dir + "/souporcell_merged_sorted_vcf.vcf"])
    subprocess.check_call(['rm', args.out_dir + '/souporcell_merged_vcf.vcf'])
    subprocess.check_call(['bgzip', args.out_dir + "/souporcell_merged_sorted_vcf.vcf"])
    final_vcf = args.out_dir + "/souporcell_merged_sorted_vcf.vcf.gz"
    subprocess.check_call(['tabix', '-p', 'vcf', final_vcf])
    for vcf in all_vcfs:
        subprocess.check_call(['rm', vcf + ".err"])
    subprocess.check_call(['rm'] + all_vcfs)
    if len(bed_files) > 0:
        for bed in bed_files:
            subprocess.check_call(['rm', bed + ".bed"])
        subprocess.check_call(['rm'] + bed_files)
        
    with open(args.out_dir + "/variants.done", 'w') as done:
        done.write(final_vcf + "\n")
    return(final_vcf)


def vartrix(args, final_vcf, final_bam):
    print("running vartrix")
    ref_mtx = args.out_dir + "/ref.mtx"
    alt_mtx = args.out_dir + "/alt.mtx"  
    with open(args.out_dir + "/vartrix.err", 'w') as err:
        with open(args.out_dir + "/vartrix.out", 'w') as out:
            subprocess.check_call(["vartrix", "--umi", "--mapq", "30", "-b", final_bam, "-c", args.barcodes, "--scoring-method", "coverage", "--threads", str(args.threads),
                "--ref-matrix", ref_mtx, "--out-matrix", alt_mtx, "-v", final_vcf, "--fasta", args.fasta], stdout = out, stderr = err)
    subprocess.check_call(['touch', args.out_dir + "/vartrix.done"])
    subprocess.check_call(['rm', args.out_dir + "/vartrix.out", args.out_dir + "/vartrix.err"])
    return((ref_mtx, alt_mtx))

def souporcell(args, ref_mtx, alt_mtx, final_vcf):
    print("running souporcell clustering")
    cluster_file = args.out_dir + "/clusters_tmp.tsv"
    with open(cluster_file, 'w') as log:
        with open(args.out_dir+"/clusters.err",'w') as err:
            directory = os.path.dirname(os.path.realpath(__file__))
            #cmd = ["souporcell.py", "-a", alt_mtx, "-r", ref_mtx, "-b", args.barcodes, "-k", args.clusters,"--restarts",str(args.restarts),
            #    "-t", str(args.threads), "-l", args.max_loci, "--min_alt", args.min_alt, "--min_ref", args.min_ref,'--out',cluster_file]
            cmd = [directory+"/souporcell/target/release/souporcell", "-k",args.clusters, "-a", alt_mtx, "-r", ref_mtx, 
                "--restarts", str(args.restarts), "-b", args.barcodes, "--min_ref", args.min_ref, "--min_alt", args.min_alt, 
                "--threads", str(args.threads)]
            print(" ".join(cmd))
            if not(args.known_genotypes == None):
                cmd.extend(['--known_genotypes', final_vcf])
                if not(args.known_genotypes_sample_names == None):
                    cmd.extend(['--known_genotypes_sample_names']+ args.known_genotypes_sample_names)
            subprocess.check_call(cmd, stdout = log, stderr = err) 
    subprocess.check_call(['touch', args.out_dir + "/clustering.done"])
    return(cluster_file)

def doublets(args, ref_mtx, alt_mtx, cluster_file):
    print("running souporcell doublet detection")
    doublet_file = args.out_dir + "/clusters.tsv"
    with open(doublet_file, 'w') as dub:
        subprocess.check_call(["troublet", "--alts", alt_mtx, "--refs", ref_mtx, "--clusters", cluster_file], stdout = dub)
    subprocess.check_call(['touch', args.out_dir + "/troublet.done"])
    return(doublet_file)

def consensus(args, ref_mtx, alt_mtx, doublet_file):
    print("running co inference of ambient RNA and cluster genotypes")
    subprocess.check_call(["consensus.py", "-c", doublet_file, "-a", alt_mtx, "-r", ref_mtx, "-p", args.ploidy,
        "--output_dir",args.out_dir,"--soup_out", args.out_dir + "/ambient_rna.txt", "--vcf_out", args.out_dir + "/cluster_genotypes.vcf", "--vcf", final_vcf])
    subprocess.check_call(['touch', args.out_dir + "/consensus.done"])



#### MAIN RUN SCRIPT
if os.path.isdir(args.out_dir):
    print("restarting pipeline in existing directory " + args.out_dir)
else:
    subprocess.check_call(["mkdir", args.out_dir])
if not args.skip_remap:
    if not os.path.exists(args.out_dir + "/fastqs.done"):
        all_fastqs = make_fastqs(args)
    else:
        all_fastqs = []
        with open(args.out_dir + "/fastqs.done") as fastqs:
            for line in fastqs:
                all_fastqs.append(line.strip())
    if not os.path.exists(args.out_dir + "/remapping.done"):
        minimap_tmp_file = remap(args, all_fastqs)
    else:
        with open(args.out_dir + "/remapping.done") as bam:
            minimap_tmp_file = bam.readline().strip()
    if not os.path.exists(args.out_dir + "/sorting.done"):
        sort(args, minimap_tmp_file)
    bam = args.out_dir + "/souporcell_minimap_tagged_sorted.bam"
else:
    bam = args.bam
if not os.path.exists(args.out_dir + "/variants.done"):
    final_vcf = freebayes(args, bam, fasta)
else:
    with open(args.out_dir + "/variants.done") as done:
        final_vcf = done.readline().strip()
if not os.path.exists(args.out_dir + "/vartrix.done"):
    vartrix(args, final_vcf, bam)
ref_mtx = args.out_dir + "/ref.mtx"
alt_mtx = args.out_dir + "/alt.mtx"
if not(os.path.exists(args.out_dir + "/clustering.done")):
    souporcell(args, ref_mtx, alt_mtx, final_vcf)
cluster_file = args.out_dir + "/clusters_tmp.tsv"
if not(os.path.exists(args.out_dir + "/troublet.done")):
    doublets(args, ref_mtx, alt_mtx, cluster_file)
doublet_file = args.out_dir + "/clusters.tsv"
if not(os.path.exists(args.out_dir + "/consensus.done")):
    consensus(args, ref_mtx, alt_mtx, doublet_file)
print("done")

#### END MAIN RUN SCRIPT        


