#!/usr/bin/python3
import sys
import os
import argparse
import traceback
import random
import math
import time
import logging
import contextlib
import json
import datetime
from datetime import timezone
import gc
import shutil
import glob
import numpy as np
import itertools
import copy
import atexit
from collections import defaultdict
from typing import Dict, List

import torch
import torch.nn
import torch.optim
import torch.distributed
import torch.multiprocessing
from torch.nn.parallel import DistributedDataParallel
from torch.optim.swa_utils import AveragedModel

import modelconfigs
from model_pytorch import Model
from metrics_pytorch import Metrics
import data_processing_pytorch

# HANDLE COMMAND AND ARGS -------------------------------------------------------------------

if __name__ == "__main__":

  description = """
  Train neural net on Go positions from npz files of batches from selfplay.
  """

  parser = argparse.ArgumentParser(description=description)
  parser.add_argument('-traindir', help='Dir to write to for recording training results', required=True)
  parser.add_argument('-datadir', help='Directory with a train and val subdir of npz data', required=True)
  parser.add_argument('-exportdir', help='Directory to export models periodically', required=True)
  parser.add_argument('-exportprefix', help='Prefix to append to names of models', required=True)
  parser.add_argument('-initial-checkpoint', help='If no training checkpoint exists, initialize from this checkpoint', required=False)
  parser.add_argument('-pos-len', help='Spatial length of expected training data', type=int, required=True)
  parser.add_argument('-batch-size', help='Batch size to use for training', type=int, required=True)
  parser.add_argument('-samples-per-epoch', help='Number of data samples to consider as one epoch', type=int, required=False)
  parser.add_argument('-multi-gpus', help='Use multiple gpus, comma-separated device ids', required=False)
  parser.add_argument('-gpu-memory-frac', help='Fraction of gpu memory to use', type=float, required=True)
  parser.add_argument('-model-kind', help='String name for what model to use', required=True)
  parser.add_argument('-lr-scale', help='LR multiplier on the hardcoded schedule', type=float, required=False)
  parser.add_argument('-gnorm-clip-scale', help='Multiplier on gradient clipping threshold', type=float, required=False)
  parser.add_argument('-sub-epochs', help='Reload training data up to this many times per epoch', type=int, required=True)
  parser.add_argument('-epochs-per-export', help='Export model once every this many epochs', type=int, required=False)
  parser.add_argument('-export-prob', help='Export model with this probablity', type=float, required=False)
  parser.add_argument('-max-epochs-this-instance', help='Terminate training after this many more epochs', type=int, required=False)
  parser.add_argument('-sleep-seconds-per-epoch', help='Sleep this long between epochs', type=int, required=False)
  parser.add_argument('-swa-sub-epoch-scale', help='Number of sub-epochs to average in expectation together for SWA', type=float, required=False)
  parser.add_argument('-max-train-bucket-per-new-data', help='When data added, add this many train rows per data row to bucket', type=float, required=False)
  parser.add_argument('-max-train-bucket-size', help='Approx total number of train rows allowed if data stops', type=float, required=False)
  parser.add_argument('-max-train-steps-since-last-reload', help='Approx total of training allowed if shuffling stops', type=float, required=False)
  parser.add_argument('-verbose', help='verbose', required=False, action='store_true')
  parser.add_argument('-no-export', help='Do not export models', required=False, action='store_true')
  args = vars(parser.parse_args())

def get_longterm_checkpoints_dir(traindir):
  return os.path.join(traindir,"longterm_checkpoints")

def make_dirs(args):
  traindir = args["traindir"]
  exportdir = args["exportdir"]

  if not os.path.exists(traindir):
    os.makedirs(traindir)
  if not os.path.exists(exportdir):
    os.makedirs(exportdir)

  longterm_checkpoints_dir = get_longterm_checkpoints_dir(traindir)
  if not os.path.exists(longterm_checkpoints_dir):
    os.makedirs(longterm_checkpoints_dir)

def multiprocessing_setup(rank: int, world_size: int):
  os.environ['MASTER_ADDR'] = 'localhost'
  os.environ['MASTER_PORT'] = '23456'
  logging.info("Running torch.distributed.init_process_group")
  torch.distributed.init_process_group("nccl", rank=rank, world_size=world_size)
  logging.info(f"Returned from torch.distributed.init_process_group, my rank = {rank}, world_size={world_size}")

def multiprocessing_cleanup():
  torch.distributed.destroy_process_group()

def dump_and_flush_json(data,filename):
  with open(filename,"w") as f:
    json.dump(data,f)
    f.flush()
    os.fsync(f.fileno())

def main(rank: int, world_size: int, args, multi_gpu_device_ids):
  traindir = args["traindir"]
  datadir = args["datadir"]
  exportdir = args["exportdir"]
  exportprefix = args["exportprefix"]
  initial_checkpoint = args["initial_checkpoint"]
  pos_len = args["pos_len"]
  batch_size = args["batch_size"]
  samples_per_epoch = args["samples_per_epoch"]
  gpu_memory_frac = args["gpu_memory_frac"]
  model_kind = args["model_kind"]
  lr_scale = args["lr_scale"]
  gnorm_clip_scale = args["gnorm_clip_scale"]
  sub_epochs = args["sub_epochs"]
  epochs_per_export = args["epochs_per_export"]
  export_prob = args["export_prob"]
  max_epochs_this_instance = args["max_epochs_this_instance"]
  sleep_seconds_per_epoch = args["sleep_seconds_per_epoch"]
  swa_sub_epoch_scale = args["swa_sub_epoch_scale"]
  max_train_bucket_per_new_data = args["max_train_bucket_per_new_data"]
  max_train_bucket_size = args["max_train_bucket_size"]
  max_train_steps_since_last_reload = args["max_train_steps_since_last_reload"]
  verbose = args["verbose"]
  no_export = args["no_export"]

  if samples_per_epoch is None:
    samples_per_epoch = 1000000
  if max_train_bucket_size is None:
    max_train_bucket_size = 1.0e30
  if epochs_per_export is None:
    epochs_per_export = 1

  num_batches_per_epoch = int(round(samples_per_epoch / batch_size))
  longterm_checkpoints_dir = get_longterm_checkpoints_dir(traindir)

  # SET UP LOGGING -------------------------------------------------------------

  logging.root.handlers = []
  logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
      logging.FileHandler(os.path.join(traindir,f"train{rank}.log"), mode="a"),
      logging.StreamHandler()
    ],
  )
  np.set_printoptions(linewidth=150)

  logging.info(str(sys.argv))

  # LOAD MODEL CONFIG -------------------------------------------------------------

  if os.path.exists(os.path.join(traindir,"model.config.json")):
    logging.info("Loading existing model config at %s" % os.path.join(traindir,"model.config.json"))
    with open(os.path.join(traindir,"model.config.json"),"r") as f:
      model_config = json.load(f)
  else:
    model_config = modelconfigs.config_of_name[model_kind]
    logging.info("Initializing with new model config")
    with open(os.path.join(traindir,"model.config.json"),"w") as f:
      json.dump(model_config,f)

  logging.info(str(model_config))

  # FIGURE OUT MULTIGPU ------------------------------------------------------------
  if world_size > 1:
    multiprocessing_setup(rank, world_size)
    atexit.register(multiprocessing_cleanup)
    assert torch.cuda.is_available()

  if True or torch.cuda.is_available():
    my_gpu_id = multi_gpu_device_ids[rank]
    torch.cuda.set_device(my_gpu_id)
    logging.info("Using GPU device: " + torch.cuda.get_device_name())
    device = torch.device("cuda", my_gpu_id)
  else:
    logging.warning("WARNING: No GPU, using CPU")
    device = torch.device("cpu")

  # LOAD MODEL ---------------------------------------------------------------------

  def get_checkpoint_path():
    return os.path.join(traindir,"checkpoint.ckpt")
  def get_checkpoint_prev_path(i):
    return os.path.join(traindir,f"checkpoint_prev{i}.ckpt")

  NUM_SHORTTERM_CHECKPOINTS_TO_KEEP = 4
  def save(model, swa_model, optimizer, metrics_obj, running_metrics, train_state, path=None):
    if rank == 0:
      state_dict = {}
      state_dict["model"] = model.state_dict()
      state_dict["optimizer"] = optimizer.state_dict()
      state_dict["metrics"] = metrics_obj.state_dict()
      state_dict["running_metrics"] = running_metrics
      state_dict["train_state"] = train_state

      if swa_model is not None:
        state_dict["swa_model"] = swa_model.state_dict()

      if path is not None:
        logging.info("Saving checkpoint: " + path)
        torch.save(state_dict, path + ".tmp")
        os.replace(path + ".tmp", path)
      else:
        logging.info("Saving checkpoint: " + get_checkpoint_path())
        for i in reversed(range(NUM_SHORTTERM_CHECKPOINTS_TO_KEEP-1)):
          if os.path.exists(get_checkpoint_prev_path(i)):
            os.replace(get_checkpoint_prev_path(i), get_checkpoint_prev_path(i+1))
        shutil.copy(get_checkpoint_path(), get_checkpoint_prev_path(0))
        torch.save(state_dict, get_checkpoint_path() + ".tmp")
        os.replace(get_checkpoint_path() + ".tmp", get_checkpoint_path())

  def get_param_groups(model):
    reg_dict : Dict[str,List] = {}
    model.add_reg_dict(reg_dict)
    param_groups = []
    if model.get_use_fixup():
      param_groups.append({
        "params": reg_dict["normal"],
        "weight_decay": 0.000001 * world_size * batch_size / 256.0,
      })
      param_groups.append({
        "params": reg_dict["output"],
        "weight_decay": 0.000001 * world_size * batch_size / 256.0,
      })
      param_groups.append({
        "params": reg_dict["noreg"],
        "weight_decay": 0.0,
      })
    else:
      param_groups.append({
        "params": reg_dict["normal"],
        "weight_decay": 0.0003 * world_size * batch_size / 256.0 * math.sqrt(lr_scale),
      })
      param_groups.append({
        "params": reg_dict["output"],
        "weight_decay": 0.000001 * world_size * batch_size / 256.0,
      })
      param_groups.append({
        "params": reg_dict["noreg"],
        "weight_decay": 0.0,
      })
    num_params = len(list(model.parameters()))
    num_reg_dict_params = len(reg_dict["normal"]) + len(reg_dict["output"]) + len(reg_dict["noreg"])
    assert num_params == num_reg_dict_params, "Reg dict does not have entries for all params in model"
    return param_groups

  def load():
    if not os.path.exists(get_checkpoint_path()):
      logging.info("No preexisting checkpoint found at: " + get_checkpoint_path())
      for i in range(NUM_SHORTTERM_CHECKPOINTS_TO_KEEP):
        if os.path.exists(get_checkpoint_prev_path(i)):
          raise Exception(f"No preexisting checkpoint found, but {get_checkpoint_prev_path(i)} exists, something is wrong with the training dir")

      if initial_checkpoint is not None:
        if os.path.exists(initial_checkpoint):
          logging.info("Using initial checkpoint: {initial_checkpoint}")
          path_to_load_from = initial_checkpoint
        else:
          raise Exception("No preexisting checkpoint found, initial checkpoint provided is invalid: {initial_checkpoint}")
      else:
        path_to_load_from = None
    else:
      path_to_load_from = get_checkpoint_path()

    if path_to_load_from is None:
      logging.info("Initializing new model!")
      model = Model(model_config,pos_len)
      model.initialize()

      model.to(device)
      if world_size > 1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[device])

      swa_model = None
      if rank == 0 and swa_sub_epoch_scale is not None:
        new_factor = 1.0 / swa_sub_epoch_scale
        ema_avg = lambda avg_param, cur_param, num_averaged: (1.0 - new_factor) * avg_param + new_factor * cur_param
        swa_model = AveragedModel(model, avg_fn=ema_avg)

      optimizer = torch.optim.SGD(get_param_groups(model), lr=1.0, momentum=0.9)
      metrics_obj = Metrics(batch_size,model)
      running_metrics = {}
      train_state = {}
      return (model, swa_model, optimizer, metrics_obj, running_metrics, train_state)
    else:
      state_dict = torch.load(path_to_load_from)
      model = Model(model_config,pos_len)

      # Strip off any "module." from when the model was saved with DDP or other things
      model_state_dict = {}
      for key in state_dict["model"]:
        old_key = key
        while key.startswith("module."):
          key = key[:7]
        model_state_dict[key] = state_dict["model"][old_key]
      model.load_state_dict(model_state_dict)

      model.to(device)
      if world_size > 1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[device])

      swa_model = None
      if rank == 0 and swa_sub_epoch_scale is not None:
        new_factor = 1.0 / swa_sub_epoch_scale
        ema_avg = lambda avg_param, cur_param, num_averaged: (1.0 - new_factor) * avg_param + new_factor * cur_param
        swa_model = AveragedModel(model, avg_fn=ema_avg)
        if "swa_model" in state_dict:
          swa_model.load_state_dict(state_dict["model"])

      optimizer = torch.optim.SGD(get_param_groups(model), lr=1.0, momentum=0.9)
      if "optimizer" in state_dict:
        optimizer.load_state_dict(state_dict["optimizer"])
      else:
        logging.info("WARNING: Optimizer not found in state dict, using fresh optimizer")

      metrics_obj = Metrics(batch_size,model)
      if "metrics" in state_dict:
        metrics_obj.load_state_dict(state_dict["metrics"])
      else:
        logging.info("WARNING: Metrics not found in state dict, using fresh metrics")

      running_metrics = {}
      if "running_metrics" in state_dict:
        running_metrics = state_dict["running_metrics"]
      else:
        logging.info("WARNING: Running metrics not found in state dict, using fresh running metrics")

      train_state = {}
      if "train_state" in state_dict:
        train_state = state_dict["train_state"]
      else:
        logging.info("WARNING: Train state not found in state dict, using fresh train state")

      return (model, swa_model, optimizer, metrics_obj, running_metrics, train_state)

  (model, swa_model, optimizer, metrics_obj, running_metrics, train_state) = load()


  if "global_step_samples" not in train_state:
    train_state["global_step_samples"] = 0
  if max_train_bucket_per_new_data is not None and "train_bucket_level" not in train_state:
    train_state["train_bucket_level"] = samples_per_epoch
  if "train_steps_since_last_reload" not in train_state:
    train_state["train_steps_since_last_reload"] = 0
  if "export_cycle_counter" not in train_state:
    train_state["export_cycle_counter"] = 0


  # Print all model parameters just to get a summary
  total_num_params = 0
  total_trainable_params = 0
  logging.info("Parameters in model:")
  for name, param in model.named_parameters():
    product = 1
    for dim in param.shape:
      product *= int(dim)
    if param.requires_grad:
      total_trainable_params += product
    total_num_params += product
    logging.info(f"{name}, {list(param.shape)}, {product} params")
  logging.info(f"Total num params: {total_num_params}")
  logging.info(f"Total trainable params: {total_trainable_params}")


  # EPOCHS AND LR ---------------------------------------------------------------------

  def update_and_return_lr():
    per_sample_lr = (0.00003 if model_config["use_fixup"] else 0.00006) * (1.0 if lr_scale is None else lr_scale)

    # Warmup for initial training
    if train_state["global_step_samples"] < 5000000:
      per_sample_lr = per_sample_lr / 3.0

    for param_group in optimizer.param_groups:
      param_group['lr'] = per_sample_lr
    return per_sample_lr

  # DATA RELOADING GENERATOR AND TRAINHISTORY ------------------------------------------------------------

  # Some globals
  last_curdatadir = None
  last_datainfo_row = 0
  trainfilegenerator = None
  num_train_files = 0
  vdatadir = None

  # Purely informational tracking of history of training
  trainhistory = {
    "history":[]
  }
  if os.path.isfile(os.path.join(traindir,"trainhistory.json")):
    logging.info("Loading existing training history: " + str(os.path.join(traindir,"trainhistory.json")))
    with open(os.path.join(traindir,"trainhistory.json")) as f:
      trainhistory = json.load(f)

  trainhistory["history"].append(("started",str(datetime.datetime.now(timezone.utc))))

  def save_history():
    if rank == 0:
      trainhistory["train_state"] = copy.deepcopy(train_state)
      trainhistory["extra_stats"] = copy.deepcopy(running_metrics)
      savepath = os.path.join(traindir,"trainhistory.json")
      savepathtmp = os.path.join(traindir,"trainhistory.json.tmp")
      dump_and_flush_json(trainhistory,savepathtmp)
      os.replace(savepathtmp,savepath)
      logging.info("Wrote " + savepath)

  def maybe_reload_training_data():
    nonlocal last_curdatadir
    nonlocal last_datainfo_row
    nonlocal trainfilegenerator
    nonlocal num_train_files
    nonlocal vdatadir

    if rank != 0:
      assert False # TODO need to figure out what to do here and for buckets and such
      return

    while True:
      curdatadir = os.path.realpath(datadir)

      # Different directory - new shuffle
      if curdatadir != last_curdatadir:
        if not os.path.exists(curdatadir):
          logging.info("Shuffled data path does not exist, there seems to be no shuffled data yet, waiting and trying again later: %s" % curdatadir)
          time.sleep(30)
          continue

        trainjsonpath = os.path.join(curdatadir,"train.json")
        if not os.path.exists(trainjsonpath):
          logging.info("Shuffled data train.json file does not exist, there seems to be no shuffled data yet, waiting and trying again later: %s" % trainjsonpath)
          time.sleep(30)
          continue

        logging.info("Updated training data: " + curdatadir)
        last_curdatadir = curdatadir

        with open(trainjsonpath) as f:
          datainfo = json.load(f)
          last_datainfo_row = datainfo["range"][1]

        if max_train_bucket_per_new_data is not None:
          if "train_bucket_level_at_row" not in trainhistory:
            train_state["train_bucket_level_at_row"] = last_datainfo_row
          if last_datainfo_row > train_state["train_bucket_level_at_row"]:
            new_row_count = last_datainfo_row - train_state["train_bucket_level_at_row"]
            logging.info("Advancing trainbucket row %.0f to %.0f, %.0f new rows" % (
              train_state["train_bucket_level_at_row"], last_datainfo_row, new_row_count
            ))
            train_state["train_bucket_level_at_row"] = last_datainfo_row
            logging.info("Fill per data %.3f, Max bucket size %.0f" % (max_train_bucket_per_new_data, max_train_bucket_size))
            logging.info("Old rows in bucket: %.0f" % train_state["train_bucket_level"])
            train_state["train_bucket_level"] += new_row_count * max_train_bucket_per_new_data
            cap = max(max_train_bucket_size, samples_per_epoch)
            if train_state["train_bucket_level"] > cap:
              train_state["train_bucket_level"] = cap
            logging.info("New rows in bucket: %.0f" % train_state["train_bucket_level"])

        logging.info("Train steps since last reload: %.0f -> 0" % train_state["train_steps_since_last_reload"])
        train_state["train_steps_since_last_reload"] = 0

        trainhistory["history"].append(("newdata",train_state["global_step_samples"],datainfo["range"]))

        # Load training data files
        tdatadir = os.path.join(curdatadir,"train")
        train_files = [os.path.join(tdatadir,fname) for fname in os.listdir(tdatadir) if fname.endswith(".npz")]
        num_train_files = len(train_files)

        # Filter down to a random subset that will comprise this epoch
        def train_files_gen():
          train_files_shuffled = train_files.copy()
          while True:
            random.shuffle(train_files_shuffled)
            for filename in train_files_shuffled:
              logging.info("Yielding training file for dataset: " + filename)
              yield filename
        trainfilegenerator = train_files_gen()

        vdatadir = os.path.join(curdatadir,"val")

      # Same directory as before, no new shuffle
      else:
        if max_train_steps_since_last_reload is not None:
          if train_state["train_steps_since_last_reload"] + 0.99 * samples_per_epoch/sub_epochs > max_train_steps_since_last_reload:
            logging.info(
              "Too many train steps since last reload, waiting 5m and retrying (current %f)" %
              train_state["train_steps_since_last_reload"]
            )
            time.sleep(300)
            continue

      break

  # METRICS -----------------------------------------------------------------------------------
  def detensorify_metrics(metrics):
    ret = {}
    for key in metrics:
      if isinstance(metrics[key], torch.Tensor):
        ret[key] = metrics[key].detach().cpu().item()
      else:
        ret[key] = metrics[key]
    return ret

  def accumulate_metrics(metric_sums, metric_weights, metrics, batch_size, decay):
    if decay != 1.0:
      for metric in metric_sums:
        if metric.endswith("_sum"):
          metric_sums[metric] *= decay
          metric_weights[metric] *= decay

    for metric in metrics:
      if not metric.endswith("_batch"):
        metric_sums[metric] += metrics[metric]
        metric_weights[metric] += batch_size
      else:
        metric_sums[metric] += metrics[metric]
        metric_weights[metric] += 1

  def log_metrics(metric_sums, metric_weights, metrics, metrics_out):
    metrics_to_print = {}
    for metric in metric_sums:
      if metric.endswith("_sum"):
        metrics_to_print[metric[:-4]] = metric_sums[metric] / metric_weights[metric]
      elif metric.endswith("_batch"):
        metrics_to_print[metric] = metric_sums[metric] / metric_weights[metric]
        metric_sums[metric] = 0.0
        metric_weights[metric] = 0.0
      else:
        metrics_to_print[metric] = metric_sums[metric]
    for metric in metrics:
      if metric not in metric_sums:
        metrics_to_print[metric] = metrics[metric]

    logging.info(", ".join(["%s = %f" % (metric, metrics_to_print[metric]) for metric in metrics_to_print]))
    if metrics_out:
      metrics_out.write(json.dumps(metrics_to_print) + "\n")
      metrics_out.flush()

  train_metrics_out = open(os.path.join(traindir,"metrics_train.json"),"w+")
  val_metrics_out = open(os.path.join(traindir,"metrics_val.json"),"w+")

  # TRAIN! -----------------------------------------------------------------------------------

  last_longterm_checkpoint_save_time = datetime.datetime.now()
  num_epochs_this_instance = 0
  print_train_loss_every_batches = 100

  running_metrics["sums"] = defaultdict(float)
  running_metrics["weights"] = defaultdict(float)

  torch.backends.cudnn.benchmark = True

  while True:
    maybe_reload_training_data()
    logging.info("GC collect")
    gc.collect()

    lr_this_epoch = update_and_return_lr()

    logging.info("=========================================================================")
    logging.info("BEGINNING NEXT EPOCH " + str(num_epochs_this_instance))
    logging.info("=========================================================================")
    logging.info("Current time: " + str(datetime.datetime.now()))
    logging.info("Global step: %d samples" % (train_state["global_step_samples"]))
    logging.info("Currently up to data row " + str(last_datainfo_row))

    if max_train_bucket_per_new_data is not None:
      if train_state["train_bucket_level"] > 0.99 * samples_per_epoch:
        logging.info("Consuming %.0f rows from train bucket (%.0f -> %.0f)" % (
          samples_per_epoch, train_state["train_bucket_level"], train_state["train_bucket_level"]-samples_per_epoch
        ))
        train_state["train_bucket_level"] -= samples_per_epoch
      else:
        logging.info(
          "Exceeding train bucket, not enough new data rows, waiting 5m and retrying (current level %f)" %
          train_state["train_bucket_level"]
        )
        time.sleep(300)
        continue

    # SUB EPOCH LOOP -----------
    batch_count_this_epoch = 0
    last_train_stats_time = time.perf_counter()
    num_batches_per_subepoch = num_batches_per_epoch / sub_epochs
    for i in range(sub_epochs):
      if i != 0:
        maybe_reload_training_data()

      # Pick enough files to get the number of batches we want
      train_files_to_use = []
      batches_to_use_so_far = 0
      for filename in trainfilegenerator:
        jsonfilename = os.path.splitext(filename)[0] + ".json"
        with open(jsonfilename) as f:
          trainfileinfo = json.load(f)

        num_batches_this_file = trainfileinfo["num_batches"]
        if num_batches_this_file <= 0:
          continue

        if batches_to_use_so_far + num_batches_this_file > num_batches_per_subepoch:
          # If we're going over the desired amount, randomly skip the file with probability equal to the
          # proportion of batches over - this makes it so that in expectation, we have the desired number of batches
          if batches_to_use_so_far > 0 and random.random() >= (batches_to_use_so_far + num_batches_this_file - num_batches_per_subepoch) / num_batches_this_file:
            break

        train_files_to_use.append(filename)
        batches_to_use_so_far += num_batches_this_file

        #Sanity check - load a max of 100000 files.
        if batches_to_use_so_far >= num_batches_per_subepoch or len(train_files_to_use) > 100000:
          break

      logging.info("Beginning training subepoch!")
      logging.info("Currently up to data row " + str(last_datainfo_row))
      train_steps_this_subepoch = 0
      for batch in data_processing_pytorch.read_npz_training_data(
          train_files_to_use, batch_size, pos_len, device, randomize_symmetries=True, model_config=model_config
      ):
        optimizer.zero_grad(set_to_none=True)
        model_outputs = model(batch["binaryInputNCHW"],batch["globalInputNC"])
        postprocessed = model.postprocess_output(model_outputs)
        metrics = metrics_obj.metrics_dict_batchwise(model,postprocessed,batch,is_training=True)

        # DDP averages loss across instances, so to preserve LR as per-sample lr, we scale by world size.
        loss = metrics["loss_sum"] * world_size
        # Now we have the reduced gradients
        loss.backward()

        gnorm_cap = (2500.0 if model_config["use_fixup"] else 4000.0) * (1.0 if gnorm_clip_scale is None else gnorm_clip_scale)
        #Loosen gradient clipping as we shift to smaller learning rates
        gnorm_cap = gnorm_cap / math.sqrt(1.0 if lr_scale is None else max(0.0000001,lr_scale))

        gnorm = torch.nn.utils.clip_grad_norm_(model.parameters(), gnorm_cap).detach().cpu().item()
        metrics["gnorm_batch"] = gnorm
        exgnorm = max(0.0, gnorm - gnorm_cap)
        metrics["exgnorm_sum"] = exgnorm * batch_size

        metrics["pslr_batch"] = lr_this_epoch

        optimizer.step()
        train_steps_this_subepoch += batch_size
        batch_count_this_epoch += 1

        metrics = detensorify_metrics(metrics)
        accumulate_metrics(running_metrics["sums"], running_metrics["weights"], metrics, batch_size, decay=0.999)
        if batch_count_this_epoch % print_train_loss_every_batches == 0:
          t1 = time.perf_counter()
          timediff = t1 - last_train_stats_time
          last_train_stats_time = t1
          metrics["time_since_last_print"] = timediff
          log_metrics(running_metrics["sums"], running_metrics["weights"], metrics, train_metrics_out)

      logging.info("Finished training subepoch!")
      train_state["train_steps_since_last_reload"] += train_steps_this_subepoch
      train_state["global_step_samples"] += train_steps_this_subepoch

      if swa_model is not None and swa_sub_epoch_scale is not None:
        swa_model.update_parameters(model)

    # END SUB EPOCH LOOP ------------

    save_history()
    save(model, swa_model, optimizer, metrics_obj, running_metrics, train_state)

    num_epochs_this_instance += 1

    if rank == 0:
      train_state["export_cycle_counter"] += 1
      logging.info("Export cycle counter = " + str(train_state["export_cycle_counter"]))

      is_time_to_export = False
      if train_state["export_cycle_counter"] >= epochs_per_export:
        if no_export:
          train_state["export_cycle_counter"] = epochs_per_export
        else:
          train_state["export_cycle_counter"] = 0
          is_time_to_export = True

      skip_export_this_time = False
      if export_prob is not None:
        if random.random() > export_prob:
          skip_export_this_time = True
          logging.info("Skipping export model this time")

      if not no_export and is_time_to_export and not skip_export_this_time:
        # Export a model for testing, unless somehow it already exists
        modelname = "%s-s%d-d%d" % (
          exportprefix,
          train_state["global_step_samples"],
          last_datainfo_row,
        )
        savepath = os.path.join(exportdir,modelname)
        savepathtmp = os.path.join(exportdir,modelname+".tmp")
        if os.path.exists(savepath):
          logging.info("NOT saving model, already exists at: " + savepath)
        else:
          os.mkdir(savepathtmp)
          logging.info("SAVING MODEL FOR EXPORT TO: " + savepath)

          save(model, swa_model, optimizer, metrics_obj, running_metrics, train_state, path=os.path.join(savepathtmp,"model.ckpt"))
          dump_and_flush_json(trainhistory,os.path.join(savepathtmp,"trainhistory.json"))
          with open(os.path.join(savepathtmp,"model.config.json"),"w") as f:
            json.dump(model_config,f)
          with open(os.path.join(savepathtmp,"saved_model","model.config.json"),"w") as f:
            json.dump(model_config,f)
          with open(os.path.join(savepathtmp,"non_swa_saved_model","model.config.json"),"w") as f:
            json.dump(model_config,f)

          time.sleep(2)
          os.rename(savepathtmp,savepath)

    # Validate
    if rank == 0:
      logging.info("Beginning validation after epoch!")
      val_files = []
      if os.path.exists(vdatadir):
        val_files = [os.path.join(vdatadir,fname) for fname in os.listdir(vdatadir) if fname.endswith(".npz")]
      if len(val_files) == 0:
        logging.info("No validation files, skipping validation step")
      else:
        with torch.no_grad():
          model.eval()
          val_metric_sums = defaultdict(float)
          val_metric_weights = defaultdict(float)
          for batch in data_processing_pytorch.read_npz_training_data(val_files, batch_size, pos_len, device, randomize_symmetries=True, model_config=model_config):
            model_outputs = model(batch["binaryInputNCHW"],batch["globalInputNC"])
            postprocessed = model.postprocess_output(model_outputs)
            metrics = metrics_obj.metrics_dict_batchwise(model,postprocessed,batch,is_training=False)
            metrics = detensorify_metrics(metrics)
            accumulate_metrics(val_metric_sums, val_metric_weights, metrics, batch_size, decay=1.0)
          log_metrics(val_metric_sums, val_metric_weights, metrics, val_metrics_out)

          model.train()

    if max_epochs_this_instance is not None and max_epochs_this_instance >= 0 and num_epochs_this_instance >= max_epochs_this_instance:
      logging.info("Hit max epochs this instance, done")
      break

    if sleep_seconds_per_epoch is None:
      time.sleep(1)
    else:
      time.sleep(sleep_seconds_per_epoch)

    if rank == 0:
      now = datetime.datetime.now()
      if now - last_longterm_checkpoint_save_time >= datetime.timedelta(hours=12):
        last_longterm_checkpoint_save_time = now
        dated_name = datetime.datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        save(model, swa_model, optimizer, metrics_obj, running_metrics, train_state, path=os.path.join(longterm_checkpoints_dir,f"{dated_name}.ckpt"))

  close(train_metrics_out)
  close(val_metrics_out)


if __name__ == "__main__":
  multi_gpus = args["multi_gpus"]
  num_gpus_used = 1
  multi_gpu_device_ids = []
  if multi_gpus is not None:
    for piece in multi_gpus.split(","):
      piece = piece.strip()
      multi_gpu_device_ids.append(int(piece))
    num_gpus_used = len(multi_gpu_device_ids)
  else:
    multi_gpu_device_ids = [0]

  make_dirs(args)
  if num_gpus_used > 1:
    torch.multiprocessing.set_start_method("spawn")
    assert False, "still need to write gradient scaling code, batch splitting, bucket logic, other multiproc handling"
    torch.multiprocessing.spawn(
      main,
      nprocs=num_gpus_used,
      args=(world_size, args, multi_gpu_device_ids)
    )
  else:
    rank = 0
    world_size = 1
    main(rank, world_size, args, multi_gpu_device_ids)
